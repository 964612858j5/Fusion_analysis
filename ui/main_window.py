"""
block01/ui/main_window.py — MainWindow.
"""

import os
import gc
import glob
import hashlib
import json
import math
import collections
import copy
import time
import weakref
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
    QApplication, QSplitter, QCheckBox, QDialog, QSizePolicy,
    QProgressDialog,
)

from ..config import (
    OME_TIFF_FILE, OUTPUT_DIR,
    NORM_LOW, NORM_HIGH, PATCH_COLORS,
)
from ..core.fusion_engine import (
    FusionEngine, FUSION_FORMULA_VERSION, fuse_channels,
)
from ..core import preview_compose, tissue_compose
from ..core.channel_remap import (
    apply_channel_remap, tint_and_sum_grays,
    compute_qupath_auto_minmax,
)
from ..core.io_loader import OMETIFFLoader
from ..utils import perf_trace
from ..utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_NUCLEI_EXPANSION,
    CELLPOSE_NUCLEI_CSD,
    CELLPOSE_NUCLEI_HQ,
    CELLPOSE_NUCLEI_HQ2,
    CELLPOSE_WHOLECELL_FUSION,
    MESMER_WHOLE_CELL,
    MESMER_NUCLEI,
    MESMER_NUCLEAR_GUIDED,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    normalize_segmentation_config,
)
from ..workers.hq_marker_segmentation import parse_hq_channels, validate_hq_channels
from ..utils.segmentation_params import (
    save_segmentation_params,
)
from ..utils.roi_project import (
    resolve_roi_context,
    mark_roi_step,
)
from ..workers.cellpose_worker import PreviewLoaderThread, run_cellpose_process
from ..workers.preview_compose_worker import PreviewComposeWorker
from ..workers.mesmer_worker import run_mesmer_patch_preview
from .step0.step0_page import Step0Page
from .block01_display import (
    Block01DisplayServices, STEP0 as _CTX_STEP0, STEP1 as _CTX_STEP1,
    STEP2 as _CTX_STEP2, STEP3 as _CTX_STEP3,
)
from .step0.config_panel import ConfigPanel
from .widgets.channel_dock import template as channel_template
from .step0.search_ctrl import SearchCtrlPanel
from .step0.result_grid import ResultGridPanel
from .step0 import overview_panel
from .step0.overview_panel import TileSelectDialog, FullFusionWorker
from .step1_5_bg_page import Step15BackgroundCorrectionPage
from .step2_page import Step2Page
from .step3_page import Step3Page
from .step4_page import Step4Page

STEP1_PATCH_PREVIEW_MAX_PX = 1024

# The overlay's channels are remapped BEFORE they are composited (so the
# expensive part can be cached), and the compositor it hands them to is
# `tint_and_sum_grays`, which cannot remap them again by construction. An
# identity WINDOW used to say the same thing to the general compositor, and
# saying it that way still cost a full remap pass per channel per frame --
# 49.96 ms at the median, measured. A parameter that means "do not do the
# expensive thing" is worse than an entry point that cannot.

# The fusion preview's per-channel signal cache, bounded twice.
#
# What it is FOR is one frame's working set: the channels the current patch
# fuses, each mapped through its current window. A 1024x1024 float32 signal
# is 4 MiB, so a 30-channel panel's working set is about 120 MiB -- and that
# is the most this may ever hold, because a window the user has dragged past
# has no reader. Keeping a version per slider step would have been 96 entries
# = ~384 MiB of history on a large slide, which is trading the stutter for
# memory pressure in a task that exists to fix behaviour on large slides.
#
# So: ONE version per (patch, channel), replaced when its window changes, and
# a byte ceiling as well as a count -- because 96 small entries and 96 large
# ones are not the same cache.
_SIGNAL_CACHE_MAX = preview_compose.DEFAULT_MAX_ENTRIES
_SIGNAL_CACHE_BYTES = preview_compose.DEFAULT_MAX_BYTES

# The preview's frame clock.
#
# MEASURED before this: every parameter event restarted a 60 ms single-shot
# timer, which is a TRAILING debounce -- while the hand keeps moving the
# publish keeps being pushed back, so a 2-3 second drag published 25 frames
# in 11.4 s (2.2 FPS) in overlay and 28 in 9.7 s (2.9 FPS) in fusion, each
# one only because an input gap happened to exceed 60 ms. The picture did
# not lag the mouse; it waited for the mouse to stop.
#
# A clock instead: the first input in an idle period draws at once (leading
# edge), and while input continues frames go out on fixed slots. 33 ms is
# ~30 FPS, chosen against the measured cost of a frame rather than an
# aspiration -- the dedup slice took the overlay's composite from ~50 ms to
# the one changed channel's ~8 ms, so a 33 ms slot is affordable and leaves
# room for the whole frame inside it. The frame is still ON the GUI thread,
# which is the next slice; this one only stops the scheduler from hiding it.
PREVIEW_FRAME_MS = 33

# Kept as the name for "one coalesced redraw", used where a delay is passed
# explicitly (a dataset switch, a session restore) rather than by the clock.
PREVIEW_COALESCE_MS = 60

# What a `step1_fusion_settings.json` has to say it is. A file of another
# version describes a contract this code does not implement, so it is refused
# rather than half-read.
FUSION_SETTINGS_VERSION = 2

STEP1_PREVIEW_OVERLAY = "overlay"
STEP1_PREVIEW_FUSION = "fusion"


def _round_display_value(value):
    """One display-parameter value, rounded so float noise cannot miss a cache
    hit while a real edit still misses it."""
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _hex_to_rgb01(value):
    """"#rrggbb" -> (r, g, b) floats in [0, 1]; white for anything unreadable."""
    text = str(value or "").lstrip("#")
    if len(text) != 6:
        return (1.0, 1.0, 1.0)
    try:
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return (1.0, 1.0, 1.0)

class _ArtifactKindMismatch(Exception):
    """The zarr on disk was written for the other Step1 output kind."""


class _GlobalWeightEditor(QWidget):
    """The shared weight editor: one view over the one set of weights.

    WHY IT EXISTS. Step1 has the channel panel; Step2 and Step3 have no
    channel controls at all, so "adjust a legal weight in any step" had no
    user-reachable entry there -- a service method called from a test is not a
    product operation. This is that entry, and it is a VIEW, not a second
    store: every spin box writes through `Block01DisplayServices
    .set_render_weight`, which goes to the one weight owner (the channel
    panel), so what the user changes here is the same number Step1 shows, a
    Save freezes and a session restores.

    One editor for the process, reachable from the Block01 toolbar in every
    step -- not four copies of a panel.
    """

    def __init__(self, services, channels, parent=None):
        super().__init__(parent)
        self._services = services
        self._spins = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        hint = QLabel("Weights apply to the fusion and to every preview.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9bd0ff;font-size:10px;")
        lay.addWidget(hint)
        for channel in channels:
            row = QHBoxLayout()
            row.setSpacing(6)
            label = QLabel(str(channel))
            label.setStyleSheet("color:#dce5ef;font-size:11px;")
            label.setMinimumWidth(90)
            row.addWidget(label)
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0.0, 1.0)
            spin.setSingleStep(0.05)
            spin.setDecimals(2)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(
                lambda value, ch=channel: self._on_spin(ch, value))
            row.addWidget(spin, stretch=1)
            self._spins[channel] = spin
            lay.addLayout(row)
        lay.addStretch()
        self._syncing = False
        self.refresh_from_state()

    def _on_spin(self, channel, value):
        if self._syncing:
            return
        self._services.set_render_weight(channel, float(value))

    def refresh_from_state(self):
        """Show what the one store says. A view, so it follows rather than
        argues: the spin boxes are set with signals suppressed."""
        self._syncing = True
        try:
            for channel, spin in self._spins.items():
                current = self._services.render_weight(channel)
                if current is None:
                    continue
                if abs(float(spin.value()) - float(current)) > 1e-9:
                    spin.setValue(float(current))
        finally:
            self._syncing = False

    def spin_for(self, channel):
        """The real control, for a caller driving a real drag."""
        return self._spins.get(channel)


class _SharedSpecTissueContext:
    """Step2's and Step3's render context: the ONE shared spec, read live.

    TWO THINGS WENT WRONG BEFORE, and this class is what each fix looks like.

    It first kept the composed `rgb` and re-published it -- a screenshot, so
    nothing the user changed afterwards could alter it. It then kept its own
    writable copy of the render spec, which diverged the other way: a weight
    moved in Step2 was overwritten by Step1's older spec the moment the user
    reached Step3, and was gone again on the way back to Step1.

    So it keeps NOTHING. Everything it draws is read at snapshot time:

      * the spec -- mode, channels, weights, fusion configuration -- from
        `Block01DisplayServices.render_spec()`, which Step1 publishes and
        which the one weight owner writes;
      * the colours and display windows from the canonical display state;
      * the arrays from the shared whole-slide service, by name.

    What it DOES have is an identity: it is registered under its own step id,
    so a frame composed for the step the user has just left fails the
    coordinator's owner and generation checks instead of landing.
    """

    def __init__(self, services, step_id):
        self._services = services
        self._step_id = step_id

    def spec(self):
        return self._services.render_spec()

    def has_spec(self):
        return self._services.render_spec() is not None

    # ── the render context ────────────────────────────────────────────
    def tissue_render_mode(self):
        spec = self.spec()
        return None if spec is None else spec.get("mode")

    def tissue_render_snapshot(self, computed_only=True):
        spec = self.spec()
        if not spec:
            return None
        if spec.get("token") != self._services.state.dataset_token():
            # The slide moved under it. A spec describing another dataset is
            # exactly the failure `b1aaac6` closed; it is dropped, not drawn.
            return None
        state = self._services.state
        # The SEMANTIC channel set, not whatever happened to be resident when
        # Step1 last composed. A channel still loading when the user walked
        # downstream used to fall out of the spec permanently: never asked
        # for again, never in the final picture.
        wanted = tuple(spec.get("channels") or ())
        arrays, loading = {}, []
        for ch in wanted:
            arr = self._services.lowres_array(ch)
            if arr is None:
                loading.append(ch)
            else:
                arrays[ch] = arr
        if loading:
            loading = list(self._services.ensure_lowres(loading))
            for ch in wanted:
                if ch not in arrays:
                    arr = self._services.lowres_array(ch)
                    if arr is not None:
                        arrays[ch] = arr
        if not arrays:
            return None
        shape = next(iter(arrays.values())).shape[:2]
        for ch in [c for c, a in arrays.items() if a.shape[:2] != shape]:
            arrays.pop(ch, None)
            loading.append(ch)
        if not arrays:
            return None
        # COMPUTED WINDOWS ONLY, and the seed goes to the seed thread.
        mappings = {}
        for ch in list(arrays):
            window = (state.mapping(ch) if computed_only
                      else state.mapping_or_seed(ch))
            if window is None:
                self._services.request_mapping_seed(ch)
                loading.append(ch)
                arrays.pop(ch, None)
            else:
                mappings[ch] = window
        if not arrays:
            return None
        mode = spec.get("mode")
        snapshot = {
            "mode": mode,
            "token": spec.get("token"),
            "arrays": arrays,
            # READ LIVE, every frame: this is what makes a colour or a
            # Min/Max/Gamma change downstream a different picture.
            "mappings": mappings,
            "colors": {ch: state.color_rgb01(ch) for ch in arrays},
            # Named, so a partial picture is visibly partial and the missing
            # channels are still owed a final frame.
            "loading": tuple(sorted(set(loading))),
        }
        if mode == tissue_compose.MODE_OVERLAY:
            weights = spec.get("weights") or {}
            snapshot["weights"] = {ch: float(weights.get(ch, 0.0))
                                   for ch in arrays}
            if not any(w > 0 for w in snapshot["weights"].values()):
                return None
        else:
            snapshot.update({
                "groups": {name: dict(chs) for name, chs
                           in (spec.get("groups") or {}).items()},
                "group_weights": dict(spec.get("group_weights") or {}),
                "nucleus": tuple(spec.get("nucleus") or ("", 0.0)),
                "fallback_norm": spec.get("fallback_norm"),
                "to_rgb": spec.get("to_rgb"),
            })
        return snapshot


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CODEX Pipeline  |  Fusion + Segmentation")
        self.resize(1400, 900)
        self.setMinimumSize(900, 650)

        # Block01's shared display layer, built BEFORE any step page exists
        # and shut down after all of them. The Intensity window and the Tissue
        # Preview are this process's, not Step0's: Step1 draws its own overlay
        # in the same popup, and Step2/Step3 navigate in it. Every step is
        # handed this object and asks it -- no step reaches into another.
        self._display = Block01DisplayServices(self)
        self.loader = None   # loaded on demand when user clicks "Load"
        self.fusion = FusionEngine()
        self.worker = None

        self._p1_diam            = None
        self._p2_params          = None
        self._seg_preview_history = {}
        self._active_segmentation_method = ""
        self._active_preview_patch = ""
        self._preview_patch_idx  = -1
        self._all_patches        = []
        self._patch_channel_cache: dict = {}
        self._patch_loaders: dict = {}
        self._patch_load_ready: set = set()
        self._patch_seg_results: dict = {}
        self._preserve_view_after_patch_load: dict = {}
        # Which preview the middle column shows.  Overlay is the landing view:
        # a fresh dataset shows DAPI and nothing else, and ticking a channel
        # changes the picture at once.
        self._step1_preview_mode = STEP1_PREVIEW_OVERLAY
        # Remapped [0,1] channel images for the overlay, keyed by
        # (patch index, channel, display window).  This is what stops a tick
        # from re-running a percentile over every channel: the blend itself is
        # sub-millisecond, the mapping is not.  Cleared wherever the raw patch
        # cache is cleared.
        # The window's OWN intermediate arrays, for the frames it still
        # draws immediately (a patch switch, a mode switch, a restore). The
        # frame clock's publishes are computed by `_compose_worker`, which
        # owns a cache of its own -- one dict mutated from two threads is a
        # different bug every time.
        self._overlay_display_cache = preview_compose.PreviewCache()
        self._signal_cache = preview_compose.PreviewCache()
        # What each patch still wants that its RUNNING loader is not fetching.
        # Per channel, not a flag: a read that fails must not take a tick made
        # while it ran down with it.
        self._pending_channel_demand: dict = {}   # patch -> {channel, ...}
        # What each running loader was asked for, so a failure can be blamed on
        # the right channels and nothing else.
        self._loader_channels: dict = {}          # patch -> {channel, ...}
        # Channels whose read failed for this patch.  Not retried on their own;
        # a user ticking the channel again clears the mark.
        self._failed_channels: dict = {}          # patch -> {channel, ...}
        # True while a saved display state is being put back, so the per-change
        # handlers stay quiet and the reconcile at the end is the only one.
        self._restoring_display_state = False
        self._preview_update_pending = False
        # The frame clock's whole state: the newest input revision not yet
        # drawn (None = nothing pending), how many inputs have been merged
        # into it, whether a frame is being drawn right now, when the last
        # one went out, and when the newest input arrived. See
        # `_schedule_preview_update`.
        self._frame_input_rev = 0
        self._frame_pending_rev = None
        self._frame_drawing_rev = None
        # The thread that turns a snapshot into pixels, created on first use.
        # `_dataset_gen` is the structural identity a result is checked
        # against: it moves whenever the pixels this window is about to draw
        # stop being the ones a pending frame was computed from.
        self._compose_worker = None
        self._retired_compose_workers = []
        self._dataset_gen = 0
        self._frame_request_id = 0
        self._frame_inflight_request = None
        self._frame_coalesced = 0
        self._frame_in_flight = False
        self._frame_last_publish = 0.0
        self._frame_input_at = 0.0
        # The settings a segmentation search or a fused.zarr run will use, as
        # opposed to the ones on screen. None until the user saves them.
        self._fusion_settings_snapshot = None
        self._fused_zarr_path    = None
        self._rois               = []
        self._active_roi         = None
        self._selected_step1_patch_idx = -1
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
        self._step1_context_ready = False
        self.step1_done          = False
        self.step2_done          = False
        self.step3_done          = False
        self.step4_done          = False
        self._current_step       = 0
        self._gui_work_dir       = ""
        # Highest Step0 dataset generation this window has already invalidated
        # for.  A repeated notification for the same committed generation is a
        # no-op (the signal is idempotent by generation, not by identity).
        self._dataset_gen_seen    = 0
        # Full-fusion job state, declared here rather than appearing when
        # `_save()` first runs, so every teardown path can reason about it.
        self._fusion_worker       = None
        self._fusion_dialog       = None
        self._fusion_token        = None   # identity of the job that may write
        self._fusion_run_id       = 0
        # Threads that were asked to stop but had not finished yet.  A strong
        # reference is kept until the thread physically ends, so the C++ object
        # is never destroyed while it is still running.
        self._retired_fusion_workers = []

        self._preload_debounce = QTimer()
        self._preload_debounce.setSingleShot(True)
        self._preload_debounce.timeout.connect(self._preload_all_patches)

        self._prev_timer = QTimer()
        self._prev_timer.setSingleShot(True)
        # PreciseTimer, because this is a frame clock: Qt's default coarse
        # timer may fire up to 5% early or late, and a slot that fires early
        # publishes a frame the budget had not paid for yet.
        self._prev_timer.setTimerType(Qt.PreciseTimer)
        # The frame clock's slot. While a control is being dragged the timer
        # is armed once and left alone, so frames go out every
        # PREVIEW_FRAME_MS and the newest state is the one drawn; it is NOT
        # restarted per event, which is what made a drag publish only after
        # the hand stopped. See `_schedule_preview_update`.
        self._prev_timer.timeout.connect(self._apply_pending_preview_update)

        self._proc_poll_timer = QTimer()
        self._proc_poll_timer.setInterval(100)
        self._proc_poll_timer.timeout.connect(self._poll_cellpose_process)

        self._step1_restore_active = False
        # The GUI thread's own heartbeat, with the switch off it is None and
        # no timer exists. It is what turns "the window froze" into a number
        # with a callback beside it.
        self._perf_heartbeat = perf_trace.start_heartbeat(self, label="main")
        # A name for this run, so lines appended to a log that already holds
        # an older run can be told apart without anyone deleting evidence.
        perf_trace.announce_run(step="startup")
        self._step1_session_timer = QTimer()
        self._step1_session_timer.setSingleShot(True)
        self._step1_session_timer.timeout.connect(self._save_step1_session)

        self._build_ui()

    def _set_gui_work_dir(self, path):
        """Keep GUI file dialogs aligned with the active ROI/step directory."""
        if not path:
            return
        path = os.path.abspath(path)
        self._gui_work_dir = path
        if hasattr(self, "_out_path_edit"):
            self._out_path_edit.setText(path)

    def current_gui_work_dir(self):
        return self._gui_work_dir or (
            self._out_path_edit.text().strip()
            if hasattr(self, "_out_path_edit") else ""
        )

    # ── UI ──────────────────────────────────────────────────────────

    def _build_ui(self):
        outer_w = QWidget()
        self.setCentralWidget(outer_w)
        outer_lay = QVBoxLayout(outer_w)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        step_bar = QHBoxLayout()
        step_bar.setContentsMargins(8, 4, 8, 4)
        self._step0_lbl = QLabel("● Step 0: Setup & Preprocessing")
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

        # ── Block01 chrome: the global windows, reachable from EVERY step ──
        #
        # Outside the stacked widget on purpose. The per-step buttons on the
        # Step0 and Step1 pages disappear with their page, so in Step2 and
        # Step3 the only way back to a closed Intensity window or Tissue
        # Preview was to walk back to Step1 -- which is not "a global window
        # for the whole session", it is a Step1 window that happens to stay
        # open. These three are always there and resolve to the same
        # instances the page buttons do.
        self._btn_global_intensity = QPushButton("Intensity…")
        self._btn_global_intensity.setToolTip(
            "The shared Intensity window. One set of Min/Max/Gamma for the "
            "whole session, editable in every step.")
        self._btn_global_tissue = QPushButton("🗺 Tissue Preview")
        self._btn_global_tissue.setToolTip(
            "The shared Tissue Preview. One window for the whole session; "
            "the picture follows whichever step you are in.")
        self._btn_global_weights = QPushButton("Weights…")
        self._btn_global_weights.setToolTip(
            "The shared channel weights. The same numbers the channel panel "
            "shows and a Save freezes.")
        for btn in (self._btn_global_intensity, self._btn_global_tissue,
                    self._btn_global_weights):
            btn.setStyleSheet(
                "QPushButton{color:#9bd0ff;font-size:10px;background:#182230;"
                "border:1px solid #354a63;border-radius:3px;padding:3px 8px;}"
                "QPushButton:hover{background:#23354a;}")
            step_bar.addWidget(btn)
        self._btn_global_intensity.clicked.connect(self._open_global_intensity)
        self._btn_global_tissue.clicked.connect(self._open_global_navigator)
        self._btn_global_weights.clicked.connect(self._open_global_weights)
        # v14.1: top-nav Skip → Step2/3/4 buttons and the Step 1.5 workflow entry
        # were removed. Direct navigation is still available via the step labels
        # above. The Step15BackgroundCorrectionPage widget and its set_context
        # injection path remain intact internally (stack index 5, reached only
        # programmatically) for the v14.1b migration into Step0.

        # (#11) The redundant "Next" button was removed — navigation is entirely
        # via clicking the step names in the top nav. The button object is kept
        # (NOT added to the bar, NOT connected) only so the shared
        # _update_next_button() state-updater and its many call sites stay
        # working unchanged; it is never shown or clickable.
        self._btn_next = QPushButton("Next")
        self._btn_next.setEnabled(False)
        self._btn_next.setVisible(False)
        self._update_next_button()
        outer_lay.addLayout(step_bar)

        self._stack = QtWidgets.QStackedWidget()
        outer_lay.addWidget(self._stack, stretch=1)

        self._step0 = Step0Page(display_services=self._display)
        self._step0.step0_complete.connect(self._on_step0_complete)
        # A committed dataset switch invalidates every Step1 fact that was
        # derived from the previous dataset.  This is a different event from
        # step0_complete (Save/handoff) and must not be folded into it.
        self._step0.dataset_committed.connect(self._on_step0_dataset_committed)
        # ROI/patch edits made anywhere are published by Step0's writer; Step1
        # re-reads them from that commit instead of keeping its own copy.
        self._step0.geometry_committed.connect(self._on_step0_geometry_committed)
        # The published handoff stopped matching the geometry in memory and no
        # replacement could be published: fail closed rather than let Step1 keep
        # using results computed for geometry that is gone.
        self._step0.handoff_invalidated.connect(self._on_step0_handoff_invalidated)
        # Min/Max/Gamma live in Step0's Intensity window; when they move, the
        # overlay's remapped pixels for that ONE channel are stale.
        self._step0.display_mapping_changed.connect(
            self._on_display_mapping_changed)
        self._step0.display_mapping_committed.connect(
            self._on_display_mapping_committed)
        self._stack.addWidget(self._step0)

        self._ome_path_edit = self._step0._ome_path_edit
        self._out_path_edit = self._step0._out_path_edit
        self._panel_csv_edit = self._step0._panel_csv_edit

        page1_w = QWidget()
        page1_w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root = QVBoxLayout(page1_w)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(4)

        title = self._make_label("Step 1 — Channel Fusion + Cellpose Grid Search", bold=True)
        root.addWidget(title)

        main_split = QSplitter(Qt.Horizontal)
        main_split.setChildrenCollapsible(False)
        self._step1_main_split = main_split

        # Left: the channel panel, plus the entry to the shared Tissue Preview.
        # ROI and patch geometry are Step0's to publish, but Step1 does edit
        # patches and may delete an ROI — both go through Step0's one model and
        # its writer, never through a Step1 copy.
        left = QWidget()
        # Wide enough for a channel row (name, slider, weight box) rather than
        # for a status line: the column's content changed, so its floor did.
        left.setMinimumWidth(300)
        left.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._step1_left_panel = left
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)
        ll.addWidget(self._make_label("① ROI / Patch Overview", bold=True))
        # The ONE Tissue Preview / ROI Navigator belongs to Block01: the
        # display services construct, hold and close it, and every step's
        # button -- this one included -- resolves to that one instance. No
        # step builds a popup, an OverviewPanel over it, or a second ROI/patch
        # model.
        self._btn_step1_tissue_nav = QPushButton("🗺 Tissue Preview / ROI Navigator")
        self._btn_step1_tissue_nav.setToolTip(
            "Open the shared Tissue Preview. One window for the whole "
            "session: the same ROI and patches, and the picture follows "
            "whichever step you are in.")
        self._btn_step1_tissue_nav.setStyleSheet(
            "QPushButton{color:#9bd0ff;font-size:10px;"
            "border:1px solid #354a63;border-radius:3px;padding:3px 8px;}"
            "QPushButton:hover{background:#182230;}"
        )
        self._btn_step1_tissue_nav.clicked.connect(self._show_tissue_navigator)
        ll.addWidget(self._btn_step1_tissue_nav)

        self._btn_step1_intensity = QPushButton("Intensity…")
        self._btn_step1_intensity.setToolTip(
            "Open the shared Intensity window on the current channel. "
            "One window for the whole session, with one set of "
            "Min/Max/Gamma — editable here and in every other step.")
        self._btn_step1_intensity.setStyleSheet(
            "QPushButton{color:#c678dd;font-size:10px;"
            "border:1px solid #4a3a5a;border-radius:3px;padding:3px 8px;}"
            "QPushButton:hover{background:#231a2a;}"
        )
        self._btn_step1_intensity.clicked.connect(self._show_intensity_window)
        ll.addWidget(self._btn_step1_intensity)

        # The one channel panel lives here, in the column the removed tissue
        # thumbnail left empty.  It is constructed in its final home rather than
        # built elsewhere and reparented, so there is never a moment with two
        # parents or two instances.
        # The SAME outer Channels frame Step0 draws: one border, one title
        # rule, one set of colours, from `channel_dock.template`. Step1 used
        # to carry a plain bold label instead, so the two steps framed the
        # same list differently.
        channels_box = QtWidgets.QGroupBox("② Channels")
        channels_box.setStyleSheet(channel_template.frame_qss())
        channels_box_lay = QVBoxLayout(channels_box)
        channels_box_lay.setContentsMargins(4, 4, 4, 4)
        channels_box_lay.setSpacing(4)
        self.config = ConfigPanel([])
        self.config.setMinimumHeight(220)
        self.config.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.config.config_changed.connect(self._on_cfg_changed)
        self.config.visibility_changed.connect(self._on_channel_visibility_changed)
        self.config.current_channel_changed.connect(self._on_current_channel_changed)
        self.config.color_changed.connect(self._on_channel_color_changed)
        # ...and the other direction: a colour picked in the Intensity window
        # or in Step0 is the same channel's colour.
        self._step0.channel_color_changed.connect(
            self._on_step0_channel_color_changed)
        channels_box_lay.addWidget(self.config, stretch=1)
        ll.addWidget(channels_box, stretch=1)
        self._step1_channels_box = channels_box

        # The commit point for everything above it. The preview follows the
        # panel live; a segmentation search and the final fused.zarr run on
        # what this button froze, so the two can be told apart.
        self._btn_save_fusion_settings = QPushButton("💾 Save Fusion Settings")
        self._btn_save_fusion_settings.setToolTip(
            "Freeze the ticked channels, their weights and their Min/Max/Gamma "
            "as the settings segmentation and fused.zarr will run on. The "
            "preview keeps following what you change afterwards.")
        self._btn_save_fusion_settings.setStyleSheet(
            "QPushButton{color:#8cf;font-size:11px;"
            "border:1px solid #8cf;border-radius:3px;padding:3px 8px;}"
            "QPushButton:hover{background:#122333;}"
            "QPushButton:disabled{color:#666;border-color:#444;}"
        )
        self._btn_save_fusion_settings.clicked.connect(self._commit_fusion_settings)
        ll.addWidget(self._btn_save_fusion_settings)
        self._fusion_settings_label = QLabel("Unsaved fusion changes")
        self._fusion_settings_label.setStyleSheet("color:#f4c45e;font-size:10px;")
        ll.addWidget(self._fusion_settings_label)

        self.roi_status = QLabel("No ROI loaded")
        self.roi_status.setAlignment(Qt.AlignCenter)
        self.roi_status.setWordWrap(True)
        self.roi_status.setStyleSheet("color:#888;font-size:10px;")
        ll.addWidget(self.roi_status)
        main_split.addWidget(left)

        mid = QSplitter(Qt.Vertical)
        mid.setChildrenCollapsible(False)
        self._step1_mid_split = mid
        pw = QWidget()
        pw.setMinimumSize(300, 300)
        pw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        pl = QVBoxLayout(pw)
        pl.setContentsMargins(0, 0, 0, 0)
        head_row = QHBoxLayout()
        self._preview_title = self._make_label("③ Preview", bold=True)
        head_row.addWidget(self._preview_title)
        head_row.addStretch()
        self._btn_mode_overlay = QPushButton("Overlay")
        self._btn_mode_fusion = QPushButton("Fusion")
        for btn, mode in ((self._btn_mode_overlay, STEP1_PREVIEW_OVERLAY),
                          (self._btn_mode_fusion, STEP1_PREVIEW_FUSION)):
            btn.setCheckable(True)
            btn.setStyleSheet(
                "QPushButton{color:#9bd0ff;background:#182230;"
                "border:1px solid #354a63;border-radius:4px;"
                "padding:2px 10px;font-size:10px;}"
                "QPushButton:checked{background:#2a5;color:#111;font-weight:bold;}")
            btn.clicked.connect(lambda _c, m=mode: self.set_preview_mode(m))
            head_row.addWidget(btn)
        self._btn_mode_overlay.setToolTip(
            "Show the ticked channels in their own colours, using the Intensity "
            "window's Min/Max/Gamma. Fusion weights are not used here.")
        self._btn_mode_fusion.setToolTip(
            "Show the Step1 fusion: nucleus, group and channel weights, "
            "cyto in red and nucleus in blue.")
        pl.addLayout(head_row)

        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Preview patch:"))
        self._patch_sel_btns = []
        self._patch_sel_container = QHBoxLayout()
        self._patch_sel_container.setSpacing(4)
        sel_row.addLayout(self._patch_sel_container)
        sel_row.addStretch()
        pl.addLayout(sel_row)

        self.patch_cache_status = QLabel(" ")
        self.patch_cache_status.setAlignment(Qt.AlignCenter)
        self.patch_cache_status.setWordWrap(False)
        self.patch_cache_status.setFixedHeight(18)
        self.patch_cache_status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.patch_cache_status.setStyleSheet("color:#777;font-size:10px;padding:0px;")
        pl.addWidget(self.patch_cache_status)

        status_row = QHBoxLayout()
        self.prev_status = QLabel("Please define a patch in Step 0 first")
        self.prev_status.setAlignment(Qt.AlignCenter)
        self.prev_status.setStyleSheet("color:#777;font-size:10px;")
        self.prev_status.setWordWrap(False)
        self.prev_status.setFixedHeight(22)
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
        self.prev_gv.setMinimumSize(300, 280)
        self.prev_gv.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.prev_vb = self.prev_gv.addViewBox()
        self.prev_vb.setAspectLocked(True)
        self.prev_vb.invertY(True)
        self.prev_img = pg.ImageItem()
        self.prev_vb.addItem(self.prev_img)
        pl.addWidget(self.prev_gv, stretch=1)
        mid.addWidget(pw)

        mid.setStretchFactor(0, 1)
        main_split.addWidget(mid)

        right_tabs = QtWidgets.QTabWidget()
        right_tabs.setStyleSheet(
            "QTabWidget::pane{border:1px solid #444;border-radius:5px;}"
            "QTabBar::tab{background:#222;color:#bbb;padding:5px 12px;border:1px solid #444;}"
            "QTabBar::tab:selected{color:#fff;border-bottom-color:#111;}"
        )
        right_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.right_tabs = right_tabs
        self._step1_right_tabs = right_tabs
        self._step1_right_split = None

        method_params_tab = QWidget()
        method_params_tab.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        method_params_lay = QVBoxLayout(method_params_tab)
        method_params_lay.setContentsMargins(0, 0, 0, 0)
        method_params_lay.setSpacing(0)
        self.method_params_tab = method_params_tab

        method_params_scroll = QtWidgets.QScrollArea()
        method_params_scroll.setWidgetResizable(True)
        method_params_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        method_params_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        method_params_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        method_params_scroll.viewport().setAutoFillBackground(False)
        method_params_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._step1_method_params_scroll = method_params_scroll

        self.search = SearchCtrlPanel()
        self.search._method_params_scroll = method_params_scroll
        self.search.setMinimumHeight(390)
        self.search.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.search.run_p1.connect(self._run_p1)
        self.search.run_p2.connect(self._run_p2)
        self.search.run_preview.connect(self._run_direct_patch_preview)
        self.search.stop.connect(self._stop)
        self.search.params_ready.connect(self._on_params_ready)
        self.search.method_changed.connect(self._on_step1_segmentation_mode_changed)
        method_params_scroll.setWidget(self.search)
        method_params_lay.addWidget(method_params_scroll)

        patch_results_tab = QWidget()
        patch_results_tab.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        patch_results_lay = QVBoxLayout(patch_results_tab)
        patch_results_lay.setContentsMargins(0, 0, 0, 0)
        patch_results_lay.setSpacing(0)
        self.patch_results_tab = patch_results_tab
        self.result_grid = ResultGridPanel()
        self.result_grid.setMinimumHeight(120)
        self.result_grid.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.result_grid.param_selected.connect(self._on_param_sel)
        patch_results_lay.addWidget(self.result_grid)

        right_tabs.addTab(method_params_tab, "Method & Parameters")
        right_tabs.addTab(patch_results_tab, "Patch Results")
        # There is no second channel view here any more.  The mirrored
        # "Channels" tab could not show the nucleus weight or any group weight,
        # so its "1.00" was never the effective weight, and its checkbox and
        # colour swatch wrote state nothing in Step1 read.  ConfigPanel is the
        # one channel state owner.
        right_tabs.setCurrentWidget(method_params_tab)
        print("[Step1-Tabs] right tabs created")
        print("[Step1-Tabs] default tab=Method & Parameters")
        main_split.addWidget(right_tabs)

        main_split.setStretchFactor(0, 2)
        main_split.setStretchFactor(1, 3)
        main_split.setStretchFactor(2, 4)
        main_split.setSizes([320, 450, 450])
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
        self._force_dapi_zarr = QCheckBox("force_overwrite_zarr")
        self._force_dapi_zarr.setChecked(False)
        self._force_dapi_zarr.setStyleSheet("color:#aaa;font-size:10px;")
        self._force_dapi_zarr.setToolTip("Regenerate Step1 fused/DAPI input zarr even when existing metadata matches.")
        bot.addWidget(self.btn_save)
        bot.addWidget(self._force_dapi_zarr)
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
        page1_scroll = QtWidgets.QScrollArea()
        page1_scroll.setWidgetResizable(True)
        page1_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        page1_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page1_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page1_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        page1_scroll.setWidget(page1_w)
        self._step1_page_widget = page1_w
        self._step1_scroll = page1_scroll
        self._stack.addWidget(page1_scroll)

        self._step2 = Step2Page()
        self._step2.go_back.connect(self._go_to_step1)
        self._step2.segmentation_done.connect(self._on_step2_complete)
        self._step2.open_qc_requested.connect(self._go_to_step3)
        self._stack.addWidget(self._step2)

        self._step3 = Step3Page()
        self._step3.go_back.connect(self._go_to_step2)
        self._step3.go_step4.connect(self._go_to_step4)
        self._stack.addWidget(self._step3)

        self._step4 = Step4Page()
        self._step4.go_back.connect(self._go_to_step3)
        self._stack.addWidget(self._step4)

        # Step 1.5 — Background Correction + Channel Conditioning / Remap.
        # Appended LAST so the existing index-based navigation (0..4) is unchanged;
        # reached by widget (setCurrentWidget), not a hardcoded index.
        self._step1_5 = Step15BackgroundCorrectionPage()
        self._stack.addWidget(self._step1_5)

        # Every step is a render context for the ONE Tissue Preview, and they
        # are all registered before any of them is made active -- so a step
        # transition never has to create one, and a step with no context
        # cannot silently leave the previous step's picture live.
        self._register_block01_contexts()

        # Land on the overlay: a fresh dataset shows DAPI and nothing else, so
        # the first tick changes the picture rather than nothing.
        self.set_preview_mode(STEP1_PREVIEW_OVERLAY, force=True)
        self._stack.setCurrentIndex(0)
        self._set_step_active(0)

    def _go_to_step0(self):
        if self._current_step == 1:
            self._stop_all_loaders()
        self._stack.setCurrentIndex(0)
        self._set_step_active(0)

    def _layout_desc(self, widget):
        if widget is None:
            return "none"
        sh = widget.sizeHint()
        mn = widget.minimumSize()
        mx = widget.maximumSize()
        sz = widget.size()
        return (
            f"size={sz.width()}x{sz.height()} "
            f"sizeHint={sh.width()}x{sh.height()} "
            f"min={mn.width()}x{mn.height()} "
            f"max={mx.width()}x{mx.height()}"
        )

    def _layout_height_desc(self, widget):
        if widget is None:
            return "none"
        return (
            f"height={widget.height()} "
            f"min={widget.minimumHeight()} "
            f"sizeHint={widget.sizeHint().height()}"
        )

    def _step1_right_child_names(self):
        tabs = getattr(self, "right_tabs", None)
        if tabs is None:
            return "none"
        return [tabs.tabText(i) for i in range(tabs.count())]

    def _show_step1_patch_results_tab(self, reason):
        tabs = getattr(self, "right_tabs", None)
        tab = getattr(self, "patch_results_tab", None)
        if tabs is not None and tab is not None:
            tabs.setCurrentWidget(tab)
            print(f"[Step1-Tabs] switched to Patch Results due to {reason}")

    def _show_step1_method_params_tab(self, reason):
        tabs = getattr(self, "right_tabs", None)
        tab = getattr(self, "method_params_tab", None)
        if tabs is not None and tab is not None:
            tabs.setCurrentWidget(tab)
            print(f"[Step1-Tabs] switched to Method & Parameters due to {reason}")

    def _log_step1_layout(self, where):
        try:
            print(f"[Layout] {where} main window size={self.size().width()}x{self.size().height()}")
            print(f"[Layout] {where} main window minimumSize={self.minimumSize().width()}x{self.minimumSize().height()}")
            print(f"[Layout] {where} Step1 sizeHint={self._layout_desc(getattr(self, '_step1_page_widget', None))}")
            print(f"[Layout] {where} left panel sizeHint/min/max={self._layout_desc(getattr(self, '_step1_left_panel', None))}")
            print(f"[Layout] {where} Fusion Preview sizeHint/min/max={self._layout_desc(getattr(self, 'prev_gv', None))}")
            print(f"[Layout] {where} Phase1 panel sizeHint/min/max={self._layout_desc(getattr(self, 'search', None))}")
            print(f"[Layout] {where} Phase2 panel sizeHint/min/max={self._layout_desc(getattr(self, 'result_grid', None))}")
            print("[Layout] called adjustSize/resize/fixedSize?=no Step1 runtime layout code")
            search = getattr(self, "search", None)
            print(f"[Layout-Step1] {where} right panel size={self._layout_desc(getattr(self, 'right_tabs', None))}")
            print(f"[Layout-Step1] {where} method group height/min/sizeHint={self._layout_height_desc(getattr(search, '_method_box', None))}")
            print(f"[Layout-Step1] {where} params scroll height/min/sizeHint={self._layout_height_desc(getattr(search, '_manual_params_scroll', None))}")
            print(f"[Layout-Step1] {where} HQ2 params scroll height/min/sizeHint={self._layout_height_desc(getattr(search, '_hq2_params_scroll', None))}")
            print(f"[Layout-Step1] {where} patch results height/min/sizeHint={self._layout_height_desc(getattr(self, 'result_grid', None))}")
            tabs = getattr(self, "right_tabs", None)
            current_tab = tabs.tabText(tabs.currentIndex()) if tabs is not None and tabs.count() else "none"
            print(f"[Step1-Layout] {where} right main children={self._step1_right_child_names()}")
            print(f"[Step1-Layout] {where} method tab size={self._layout_desc(getattr(self, 'method_params_tab', None))}")
            print(f"[Step1-Layout] {where} patch results tab size={self._layout_desc(getattr(self, 'patch_results_tab', None))}")
            print(f"[Layout-Step1] {where} splitter sizes=not-used tab={current_tab}")
        except Exception as e:
            print(f"[Layout] log failed: {e}")

    def _show_tissue_navigator(self):
        """Open the shared Tissue Preview from Step1.

        Through Block01's display layer, not through Step0. The popup is one
        window for the whole process and the layer is what resolves every
        step's button to it; a `self._step0.<...>` here would make Step0 a
        service locator for a window Step1, Step2 and Step3 use as much.
        """
        self._display.show_navigator(_CTX_STEP1, roi_policy="delete_only",
                                     patch_editable=True)

    # ── Step1's render context (what the coordinator asks this window) ──

    def tissue_render_mode(self):
        return (tissue_compose.MODE_FUSION
                if self._step1_preview_mode == STEP1_PREVIEW_FUSION
                else tissue_compose.MODE_OVERLAY)

    def tissue_render_snapshot(self, computed_only=True):
        """Everything Step1's whole-slide frame needs, as plain values.

        THE WHOLE SLIDE, not the current patch: this is a navigation
        thumbnail, and stretching a patch across it would be a picture of
        somewhere else. The arrays come from Block01's low-resolution service
        -- the same ones Step0's thumbnail and the display seed use -- so a
        weight change re-tints resident pixels and reads nothing.

        ONLY THE CHANNELS THIS FRAME NEEDS. Overlay asks for what is ticked;
        fusion asks for the effective config's contributors plus the nucleus.
        A 29- or 57-channel panel must not become 29 or 57 reads because one
        weight moved.

        Returns None when nothing is drawable yet. Channels that have not
        arrived are named in `loading` and requested in the background; the
        frame draws what IS there rather than holding the popup empty or
        leaving the previous step's picture up.
        """
        mode = self.tissue_render_mode()
        state = self._display.state
        if mode == tissue_compose.MODE_OVERLAY:
            wanted = [ch for ch in self.config.visible_channels()]
            weights = {ch: self._overlay_weight(ch) for ch in wanted}
        else:
            effective = self._effective_fusion_config()
            groups = {name: dict(data.get("channels") or {})
                      for name, data in (effective.get("groups") or {}).items()}
            group_weights = {
                name: float(data.get("group_weight", 1.0) or 0.0)
                for name, data in (effective.get("groups") or {}).items()}
            nuc = effective.get("nucleus") or {}
            nuc_ch = str(nuc.get("channel", "") or "")
            wanted = list({nuc_ch} if nuc_ch else set())
            for ch_weights in groups.values():
                wanted.extend(ch_weights.keys())
            wanted = sorted(set(ch for ch in wanted if ch))
        if not wanted:
            return None
        arrays, loading = {}, []
        for ch in wanted:
            arr = self._display.lowres_array(ch)
            if arr is None:
                loading.append(ch)
            else:
                arrays[ch] = arr
        if loading:
            loading = list(self._display.ensure_lowres(loading))
            for ch in wanted:
                if ch not in arrays:
                    arr = self._display.lowres_array(ch)
                    if arr is not None:
                        arrays[ch] = arr
        if not arrays:
            return None
        # Every array in one frame has to be the same picture of the same
        # slide; a channel off another pyramid level would be composited at
        # the wrong scale.
        shape = next(iter(arrays.values())).shape[:2]
        mismatched = [ch for ch, arr in arrays.items()
                      if arr.shape[:2] != shape]
        for ch in mismatched:
            arrays.pop(ch, None)
            loading.append(ch)
        if not arrays:
            return None
        # COMPUTED WINDOWS ONLY: a channel without one is named as loading
        # and its seed goes to the seed thread. A percentile here would be a
        # whole-slide pass inside a snapshot callback.
        mappings = {}
        for ch in list(arrays):
            window = (state.mapping(ch) if computed_only
                      else state.mapping_or_seed(ch))
            if window is None:
                self._display.request_mapping_seed(ch)
                loading.append(ch)
                arrays.pop(ch, None)
            else:
                mappings[ch] = window
        if not arrays:
            return None
        token = self._display.state.dataset_token()
        snapshot = {
            "mode": mode,
            "token": token,
            "arrays": arrays,
            "mappings": mappings,
            "colors": {ch: state.color_rgb01(ch) for ch in arrays},
            "loading": tuple(sorted(set(loading))),
        }
        # The SPEC: the semantic description of this picture, published so
        # every step draws the same one and can COMPOSE IT AGAIN when a
        # colour, a display window or a weight moves. Not the pixels -- pixels
        # cannot answer a later change, which is what made Step2/Step3 go
        # dead.
        #
        # `wanted`, not `arrays`: the channels this picture is OF, whether or
        # not they have arrived. Writing the resident set here meant a channel
        # still loading when the user walked downstream dropped out of the
        # configuration for good -- never requested again, never in the final
        # frame.
        spec = {"mode": mode, "token": token,
                "channels": tuple(sorted(wanted)),
                "weights": {}, "groups": {}, "group_weights": {},
                "nucleus": ("", 0.0),
                "fallback_norm": None, "to_rgb": None}
        if mode == tissue_compose.MODE_OVERLAY:
            snapshot["weights"] = {ch: weights.get(ch, 0.0) for ch in arrays}
            spec["weights"] = {ch: float(weights.get(ch, 0.0))
                               for ch in wanted}
            if not any(w > 0 for w in snapshot["weights"].values()):
                # Everything ticked is at zero: an empty picture is a real
                # answer here, and the composer returns None for it. Said as
                # "nothing to draw" rather than as a frame of black.
                return None
        else:
            fusion_bits = {
                "groups": groups,
                "group_weights": group_weights,
                "nucleus": (nuc_ch, float(nuc.get("weight", 0.0) or 0.0)),
                # The loader's own normalisation and the engine's own
                # cyto/nucleus-to-RGB step, passed in rather than
                # reimplemented: the thumbnail and the patch preview are the
                # same fusion or they are two answers.
                "fallback_norm": (self.loader._norm if self.loader is not None
                                  else (lambda a: a)),
                "to_rgb": self.fusion.to_rgb,
            }
            snapshot.update(fusion_bits)
            spec.update(fusion_bits)
        # Published, not kept: there is ONE spec for the process and the
        # downstream contexts read it rather than copying it.
        self._display.publish_render_spec(spec)
        return snapshot

    def tissue_render_spec(self):
        """The spec every step draws. None until Step1 has composed once."""
        return self._display.render_spec()

    def set_render_weight(self, channel, weight):
        """THE weight write, wherever in Block01 it came from.

        The channel panel is the weight's owner -- it is what a Save freezes
        and what a session restores -- so a weight moved in Step2 or Step3
        moves the same row Step1 shows. That is the whole difference between
        a global weight and a per-step pixel effect: walking on to Step3 and
        back to Step1 finds the number the user chose, because there was only
        ever one place it was written.

        Moving the row re-publishes the render spec through the ordinary
        `config_changed` path, so every reader picks it up without this
        method telling any of them.
        """
        rows = getattr(self.config, "_rows", {})
        if not channel or channel not in rows:
            return False
        if self.config.channel_weight(channel) == float(weight):
            return False
        rows[channel].spin.setValue(float(weight))
        # Step1's own snapshot is what publishes the spec, and it only runs
        # when Step1 is the active context. From a downstream step the spec
        # has to be refreshed here, or the weight would move the panel and
        # not the picture.
        if self._display.coordinator.active_context_id() != _CTX_STEP1:
            self._refresh_published_render_spec()
        return True

    def render_weight(self, channel):
        return self.config.channel_weight(channel)

    def _refresh_published_render_spec(self):
        """Re-publish the spec from the CURRENT panel state.

        Used when the weights move while a downstream step is drawing: Step1
        is not composing, so nothing else would rebuild the spec, and the
        shared one would keep the weights the user has just changed away
        from.
        """
        spec = self._display.render_spec()
        if not spec:
            return
        spec = dict(spec)
        if spec.get("mode") == tissue_compose.MODE_OVERLAY:
            spec["weights"] = {ch: self._overlay_weight(ch)
                               for ch in spec.get("channels") or ()}
        else:
            effective = self._effective_fusion_config()
            spec["groups"] = {
                name: dict(data.get("channels") or {})
                for name, data in (effective.get("groups") or {}).items()}
            spec["group_weights"] = {
                name: float(data.get("group_weight", 1.0) or 0.0)
                for name, data in (effective.get("groups") or {}).items()}
            nuc = effective.get("nucleus") or {}
            spec["nucleus"] = (str(nuc.get("channel", "") or ""),
                               float(nuc.get("weight", 0.0) or 0.0))
        self._display.publish_render_spec(spec)

    # ── the global entries, from the Block01 chrome ───────────────────
    def _open_global_intensity(self):
        """Open the ONE Intensity window, on the channel being edited.

        The same call in every step. Which channel that is comes from the
        channel panel, which is the one answer for the process; a step with
        no channel list of its own still opens the window on it.
        """
        return self._display.show_intensity(self.config.current_channel())

    def _open_global_navigator(self):
        """Open the ONE Tissue Preview, under the active step's policy."""
        return self._display.show_navigator()

    def _open_global_weights(self):
        """Open the ONE weight editor."""
        return self._display.show_weight_editor()

    def weight_editor_widget(self):
        """The weight editor content port: a VIEW over the one weight store."""
        channels = [ch for ch in (self.config.all_channels or [])]
        if not channels:
            return None
        return _GlobalWeightEditor(self._display, channels)

    def _refresh_weight_editor(self):
        panel = self._display.weight_editor_panel()
        refresh = getattr(panel, "refresh_from_state", None)
        if refresh is not None:
            refresh()

    def _register_block01_contexts(self):
        """Register every step as a render context for the one Tissue Preview.

        Step1 draws its overlay or its fusion. Step2 and Step3 consume
        geometry they did not produce and navigate in a picture they do not
        choose, so their context PINS the last frame Step1 published: an
        explicit choice, recorded here and tested, rather than "whatever
        Step0's fields happen to say", which is what reading them would be.
        """
        coordinator = self._display.coordinator
        coordinator.register_context(_CTX_STEP1, self)
        self._display.set_weight_owner(self)
        self._display.set_weight_editor_content(self)
        self._downstream_contexts = {
            _CTX_STEP2: _SharedSpecTissueContext(self._display, _CTX_STEP2),
            _CTX_STEP3: _SharedSpecTissueContext(self._display, _CTX_STEP3),
        }
        for step_id, context in self._downstream_contexts.items():
            coordinator.register_context(step_id, context)
        # The canonical colour settled: Step1's rows and its viewer follow it
        # like every other view, without Step0 and Step1 pushing at each other.
        self._display.state.color_changed.connect(
            self._on_shared_channel_color_changed)
        self.config.set_display_state(self._display.state)


    def _on_shared_channel_color_changed(self, channel, _hexc):
        """Block01 settled a colour: Step1's pictures take it.

        One refresh, not two. The panel row and Step0's widgets are mirrors
        updated by their own subscriptions, and the Tissue Preview is asked
        by the shared state itself; what is left for this window is the patch
        viewer.
        """
        if self._restoring_display_state:
            return
        if self._current_step == 1:
            self._schedule_preview_update()

    def _on_step0_geometry_committed(self, payload):
        """Adopt a geometry-only commit published by Step0.

        The payload is what Step0's writer just put on disk, so Step1 follows
        the new authority rather than keeping a second copy of the geometry.
        Bound by manifest path: a commit for a handoff this window is not bound
        to is ignored rather than half-applied.
        """
        payload = dict(payload or {})
        bound = str((self.step0_output or {}).get("step0_manifest_path") or "")
        incoming = str(payload.get("step0_manifest_path") or "")
        if not bound or not incoming:
            return
        if os.path.abspath(bound) != os.path.abspath(incoming):
            return

        rois = [dict(r) for r in (payload.get("rois") or [])]
        patches = []
        for item in payload.get("patches") or []:
            bbox = item.get("bbox_fullres") if isinstance(item, dict) else item
            if bbox and len(bbox) == 4:
                patches.append(tuple(int(v) for v in bbox))

        self._rois = rois
        self._active_roi = rois[0] if rois else None
        if self._active_roi:
            patches = self._filter_patches_to_roi(patches, self._active_roi)
        self.step0_output["rois"] = list(self._rois)
        self.step0_output["patches"] = list(patches)
        self._on_rois_changed(self._rois)
        # `_on_patches` is the single sink: it drops every cache, result and
        # history that belonged to a patch whose identity or bbox moved.
        self._on_patches(patches)
        print(f"[Step1] adopted Step0 geometry commit: patches={len(patches)}")

    def _on_step0_dataset_committed(self, info):
        """A different dataset is now Step0's committed dataset.

        Everything Step1 holds was derived from the PREVIOUS dataset, so it is
        dropped here rather than at the next Save.  Fail-closed: both readiness
        flags go false, so `_go_to_step1` refuses entry until the authoritative
        reader has accepted the new dataset's handoff.

        Idempotent by generation: Step0 only emits this from the tail of its
        commit block, and a repeat for a generation already invalidated here is
        a no-op.  A FAILED load returns before that commit block, so this never
        runs for a switch that did not happen and the previous dataset stays
        current with its readiness untouched.
        """
        info = dict(info or {})
        gen = int(info.get("gen") or 0)
        if gen and gen <= self._dataset_gen_seen:
            return
        self._dataset_gen_seen = max(gen, self._dataset_gen_seen)

        # The snapshot described the previous slide's channels.
        self._forget_fusion_settings("another dataset was committed")
        self.step0_done = False
        self._step1_context_ready = False
        self.step1_done = False
        self._discard_step1_dataset_state()
        # Standing on a downstream page whose every field was just cleared is
        # not a locked page, it is an empty one.  Send the user back to the step
        # that owns the new dataset.
        self._return_to_step0(f"dataset switch committed (gen={gen})")
        self._update_next_button()
        print(f"[Step1] dataset switch committed (gen={gen}); Step1 context invalidated")

    def _on_step0_handoff_invalidated(self, payload):
        """The published handoff no longer describes the geometry in memory.

        The user's new ROI/patch stays exactly where they put it — it is
        Step0's staged geometry now — but every Step1 fact derived from the
        OLD geometry (results, caches, the corrected zarr binding, readiness)
        has stopped being true, so it goes.  Bound by manifest path: an
        invalidation for a handoff this window is not bound to is ignored.
        """
        payload = dict(payload or {})
        bound = str((self.step0_output or {}).get("step0_manifest_path") or "")
        incoming = str(payload.get("step0_manifest_path") or "")
        if not bound or not incoming:
            return
        if os.path.abspath(bound) != os.path.abspath(incoming):
            return

        reason = str(payload.get("reason") or "")
        message = str(payload.get("message") or
                      "The Step0 handoff is no longer valid; run Step0 Save.")
        # Settings frozen against a handoff that no longer holds describe a
        # configuration nothing can reproduce.
        self._forget_fusion_settings("the handoff no longer holds")
        self.step0_done = False
        self._step1_context_ready = False
        self.step1_done = False
        self._discard_step1_dataset_state(status_text=message)
        # Step2, Step3, Step4 and Step1.5 all consume this handoff too, so an
        # invalidation returns to Step0 from any of them, not only from Step1.
        self._return_to_step0(f"handoff invalidated ({reason})")
        self._update_next_button()
        print(f"[Step1] handoff invalidated ({reason}); Step1 locked")

    def _discard_step1_dataset_state(
            self,
            status_text="Dataset changed — Step1 is locked until the new Step0 "
                        "handoff is loaded."):
        """Drop every Step1 fact that belonged to the previous dataset.

        Deliberately field-by-field rather than a blanket reset: the listed
        fields are exactly those keyed by the old dataset's identity, geometry
        or patch indices.  Step-navigation state, layout and Step0's own state
        are not ours to clear.
        """
        # 1. Silence anything that could still write Step1 state.  Stopping the
        #    loaders also DISCONNECTS them, so a thread that refuses to finish
        #    can no longer deliver the old dataset's pixels into the new one.
        self._stop_all_loaders()
        self._preload_debounce.stop()
        # A new dataset's pixels are not the old one's: bump the generation
        # BEFORE anything else, so a frame already being composed can no
        # longer be published however late it arrives.
        self._dataset_gen += 1
        self._reset_frame_clock()
        self._stop_compose_worker("the Step1 context was discarded")
        self._step1_session_timer.stop()
        self._retire_fusion_worker("the Step1 context was discarded")
        if self.proc is not None:
            self._stop()

        # 2. Data-source bindings.
        self.loader = None
        self.step0_output = {}
        self.step1_output = None
        self._corrected_zarr_path = ""
        self._corrected_zarr_mode = ""
        self._corrected_decisions = {}
        self._fused_zarr_path = None

        # 3. Geometry (authoritative copy lives in the new dataset's manifest).
        self._rois = []
        self._active_roi = None
        self._all_patches = []

        # 4. Everything keyed by a patch index.
        self._patch_channel_cache.clear()
        self._overlay_display_cache.clear()
        self._signal_cache.clear()
        self._patch_load_ready.clear()
        self._patch_seg_results.clear()
        self._preserve_view_after_patch_load.clear()
        self._pending_channel_demand.clear()
        self._loader_channels.clear()
        self._failed_channels.clear()
        self._preview_patch_idx = -1
        self._selected_step1_patch_idx = -1
        self._step1_preview_mode = STEP1_PREVIEW_OVERLAY

        # 5. Everything keyed by a patch name / by the old dataset's results.
        self._seg_preview_history = {}
        self._active_preview_patch = ""
        self._active_segmentation_method = ""
        self._p2_params = None
        self._p1_diam = None
        self._params_source = None

        # 6. On-screen remains of the old dataset.
        self._rebuild_patch_buttons([])
        self.prev_img.clear()
        self.result_grid.setup_grid(0, [], "")
        self.btn_save.setEnabled(False)
        self._fusion_bar_widget.setVisible(False)
        self.patch_cache_status.setText(" ")
        self.roi_status.setText("No ROI loaded")
        self.prev_status.setText(status_text)

        # 7. Downstream pages, through their public entry points only.
        if hasattr(self, "_step2"):
            self._step2.set_rois([])
            if hasattr(self._step2, "set_roi_context"):
                self._step2.set_roi_context(roi_id="", roi_dir="", step2_dir="")

    def _on_step0_complete(self, payload):
        global OME_TIFF_FILE, OUTPUT_DIR
        # The payload carries only a loader hint and the path to the committed
        # manifest.  _load_step0_roi_result is the single authoritative reader.
        self.step0_output = dict(payload or {})
        self.loader = self.step0_output.get("loader")
        OME_TIFF_FILE = self.step0_output.get("ome_tiff_path", OME_TIFF_FILE)
        OUTPUT_DIR = self.step0_output.get("step1_dir") or self.step0_output.get("output_dir", OUTPUT_DIR)
        self.step0_done = False
        self._step1_context_ready = False

        accepted = self._load_step0_roi_result(auto=True)
        if accepted is True:
            # Step0 is considered complete only after a valid handoff was
            # accepted. Step1 readiness is tracked separately below.
            self.step0_done = True
            self._step1_context_ready = True
        else:
            self.step0_done = False
            self._step1_context_ready = False
            self.prev_status.setText("Step0 saved, but its handoff could not be loaded. Step1 is not ready.")

        self.step1_done = False
        self._step2._out_edit.setText(self.step0_output.get("step2_dir") or OUTPUT_DIR)
        self._step4._ome_edit.setText(OME_TIFF_FILE)
        self._step4._out_edit.setText(self.step0_output.get("step2_dir") or OUTPUT_DIR)
        # SAVE-ONLY: stay in Step0; the user enters Step1 explicitly.
        self._update_next_button()
        self._log_step1_layout("Step0 complete (save-only, no auto-jump)")

    def _load_step0_roi_result(self, _checked=False, auto=False):
        global OME_TIFF_FILE, OUTPUT_DIR
        print("[Step1] loading Step0 ROI result")

        def _schema(value, default=1):
            try:
                return int(value if value is not None else default)
            except (TypeError, ValueError):
                return None

        def _path(value, base=None):
            """Resolve a declared path without turning an empty value into cwd."""
            if value is None:
                return ""
            try:
                value = os.fspath(value).strip()
            except (TypeError, ValueError):
                return ""
            if not value:
                return ""
            if base and not os.path.isabs(value):
                value = os.path.join(base, value)
            return os.path.abspath(value)

        payload_schema = _schema(self.step0_output.get("handoff_schema_version", 1))
        if payload_schema is None:
            print("[Step1] invalid handoff schema hint")
            return False
        # A restart/manual v2 restore may provide only the exact manifest
        # path.  Let this authoritative reader create the loader from that
        # manifest; broad directory bootstrap is only a legacy fallback.
        has_explicit_manifest = bool(self.step0_output.get("step0_manifest_path"))
        if self.loader is None and not has_explicit_manifest:
            if not self._bootstrap_step1_context_from_disk(auto=auto):
                if not auto:
                    QMessageBox.warning(
                        self,
                        "Step1",
                        "Load Step0 first, or select a Step0 output directory "
                        "that contains correction_config.json / roi_config.json / corrected_channels.zarr.",
                    )
                return False

        out_dir = self.step0_output.get("step0_dir") or self.step0_output.get("output_dir") or OUTPUT_DIR
        # The payload is only a hint. Once the manifest is read, its schema
        # controls all path and artifact decisions, including restart/manual
        # loads where the payload was reconstructed from disk.
        declared_manifest = self.step0_output.get("step0_manifest_path")
        ctx = resolve_roi_context(out_dir, OUTPUT_DIR)
        step0_dir = (ctx or {}).get("step_dirs", {}).get("step0", out_dir)
        step1_dir = (ctx or {}).get("step_dirs", {}).get("step1", out_dir)
        step2_dir = (ctx or {}).get("step_dirs", {}).get("step2", out_dir)
        # New handoffs name the exact manifest. Never re-resolve through the
        # active-ROI index, which could point at a sibling ROI.
        manifest_path = _path(declared_manifest) or _path(
            (ctx or {}).get("step0_manifest_path")) or _path(
                os.path.join(step0_dir, "step0_roi_result.json"))
        manifest = {}
        manifest_exists = os.path.isfile(manifest_path)
        if payload_schema >= 2 and not manifest_exists:
            print(f"[Step1] authoritative handoff manifest missing: {manifest_path}")
            return False
        if manifest_exists:
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                if not isinstance(manifest, dict):
                    raise ValueError("manifest root must be an object")
                # Schema-v2 manifest paths are authoritative; do not let an
                # active-ROI resolver redirect this load to a sibling session.
                manifest_base = os.path.dirname(manifest_path)
                step0_dir = _path(manifest.get("step0_dir") or manifest.get("output_dir"), manifest_base) or step0_dir
                step1_dir = _path(manifest.get("step1_dir"), manifest_base) or (
                    os.path.join(_path(manifest["roi_dir"], manifest_base), "step1")
                    if manifest.get("roi_dir") else step1_dir
                )
                step2_dir = _path(manifest.get("step2_dir"), manifest_base) or (
                    os.path.join(_path(manifest["roi_dir"], manifest_base), "step2")
                    if manifest.get("roi_dir") else step2_dir
                )
                out_dir = step0_dir
            except Exception as e:
                print(f"[Step1] failed to load step0_roi_result.json: {e}")
                return False
        manifest_schema = _schema(manifest.get("handoff_schema_version", 1)) if manifest_exists else payload_schema
        if manifest_schema is None:
            print("[Step1] invalid handoff schema in manifest")
            return False
        # A v1 manifest remains legacy-compatible even if a stale payload
        # advertised v2 only when the payload itself was legacy. A payload
        # that explicitly declares v2 must never be downgraded by a stale v1
        # manifest.
        handoff_schema = manifest_schema if manifest_exists else payload_schema
        if payload_schema >= 2 and manifest_exists and handoff_schema < 2:
            print("[Step1] v2 handoff payload cannot use a v1 manifest")
            return False
        if handoff_schema >= 2 and not manifest_exists:
            print("[Step1] authoritative handoff requires a readable manifest")
            return False
        print(f"[Step1] manifest found={bool(manifest)}")
        if handoff_schema >= 2:
            identity = manifest.get("source_identity")
            if not isinstance(identity, dict):
                print("[Step1] authoritative handoff has no source identity")
                return False
            manifest_base = os.path.dirname(manifest_path)
            raw_identity_path = _path(identity.get("dataset_path"), manifest_base)
            raw_manifest_path = _path(manifest.get("raw_ome_path"), manifest_base)
            if not raw_identity_path or not raw_manifest_path or raw_identity_path != raw_manifest_path:
                print("[Step1] authoritative handoff source paths disagree")
                return False
            if not identity.get("dataset_fingerprint") or not os.path.exists(raw_identity_path):
                print("[Step1] authoritative handoff source is missing")
                return False
            try:
                st = os.stat(raw_identity_path)
                actual_fp = f"{st.st_size}:{st.st_mtime_ns}"
            except OSError:
                return False
            if actual_fp != str(identity.get("dataset_fingerprint", "")):
                print("[Step1] authoritative handoff source identity mismatch")
                return False
        print("[Step1] loading ROI context")
        print(f"[Step1] roi_id={manifest.get('roi_id') or (ctx or {}).get('roi_id', '')}")
        print(f"[Step1] step0_result={manifest_path}")

        manifest_base = os.path.dirname(manifest_path)
        raw_ome = _path(manifest.get("raw_ome_path"), manifest_base)
        if handoff_schema >= 2:
            # A v2 handoff cannot silently reuse a loader from another dataset.
            if not raw_ome:
                print("[Step1] authoritative handoff has no raw OME path")
                return False
            raw_ome = os.path.abspath(raw_ome)
            if not os.path.exists(raw_ome):
                print(f"[Step1] authoritative raw OME is missing: {raw_ome}")
                return False
        if raw_ome and os.path.exists(raw_ome) and (
            self.loader is None or _path(getattr(self.loader, "filepath", "")) != raw_ome
        ):
            try:
                OME_TIFF_FILE = raw_ome
                OUTPUT_DIR = step1_dir or out_dir
                self.loader = OMETIFFLoader(raw_ome)
                self.step0_output["loader"] = self.loader
                self.step0_output["ome_tiff_path"] = raw_ome
            except Exception:
                print(f"[Step1] failed to load raw OME from manifest:\n{traceback.format_exc()}")
                if handoff_schema >= 2:
                    return False
        if handoff_schema >= 2 and self.loader is None:
            print("[Step1] authoritative handoff has no usable loader")
            return False
        print(f"[Step1] loader initialized={self.loader is not None}")
        if self.loader is not None:
            try:
                print(f"[Step1] loader channels count={len(self.loader.channel_names())}")
            except Exception as e:
                print(f"[Step1] loader channel parse failed: {e}")
                if handoff_schema >= 2:
                    return False

        if handoff_schema >= 2:
            corr_path = _path(manifest.get("corrected_zarr_path"), manifest_base)
            cfg_path = _path(manifest.get("correction_config_path"), manifest_base)
            roi_path = _path(manifest.get("roi_config_path"), manifest_base)
            patch_path = _path(manifest.get("patch_config_path"), manifest_base)
            required_paths = (cfg_path, roi_path, patch_path, corr_path)
            if any(not path or not os.path.exists(path) for path in required_paths):
                print("[Step1] authoritative handoff artifact is missing")
                return False
            # Remap is optional, but when the manifest declares one both its
            # path and semantic hash are authoritative.  Never substitute a
            # Step0 widget/legacy Step1.5 path when this check fails.
            remap_path = _path(manifest.get("channel_remap_config_path"), manifest_base)
            remap_hash = str(manifest.get("channel_remap_config_hash") or "")
            if not remap_path and not remap_hash:
                print("[Step1] authoritative remap declaration is incomplete")
                return False
            if remap_path:
                if not os.path.isfile(remap_path):
                    if remap_hash:
                        print("[Step1] authoritative remap config is missing")
                        return False
                else:
                    try:
                        from ..utils.channel_remap_config import (
                            load_channel_remap_config, channel_remap_config_hash,
                        )
                        remap_cfg = load_channel_remap_config(remap_path)
                        if not remap_hash or channel_remap_config_hash(remap_cfg) != remap_hash:
                            print("[Step1] authoritative remap config hash mismatch")
                            return False
                    except Exception as e:
                        print(f"[Step1] authoritative remap config failed: {e}")
                        return False
            elif remap_hash:
                print("[Step1] authoritative remap hash has no path")
                return False
        else:
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

        correction_config = None if handoff_schema >= 2 else self.step0_output.get("correction_config")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    correction_config = json.load(f)
                if handoff_schema >= 2 and not isinstance(correction_config, dict):
                    raise ValueError("correction config root must be an object")
            except Exception as e:
                print(f"[Step1] failed to load correction_config.json: {e}")
                if handoff_schema >= 2:
                    return False

        rois = [] if handoff_schema >= 2 else list(self.step0_output.get("rois") or self._rois or [])
        if os.path.exists(roi_path):
            try:
                with open(roi_path, "r", encoding="utf-8") as f:
                    rois = json.load(f)
                if handoff_schema >= 2 and not isinstance(rois, list):
                    raise ValueError("ROI config root must be an array")
            except Exception as e:
                print(f"[Step1] failed to load roi_config.json: {e}")
                if handoff_schema >= 2:
                    return False

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
                if handoff_schema >= 2:
                    return False
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
        decisions.update((manifest.get("corrected_decisions") if handoff_schema >= 2 else self.step0_output.get("corrected_decisions")) or {})
        self._corrected_decisions = decisions
        try:
            self.loader.set_correction_config(correction_config)
            self.loader.set_corrected_zarr_store(self._corrected_zarr_path, decisions)
        except Exception as e:
            print(f"[Step1] loader correction handoff failed: {e}")
            if handoff_schema >= 2:
                return False

        self._rois = list(rois or [])
        self._active_roi = self._rois[0] if self._rois else None
        if corrected_mode == "roi_only" and not self._active_roi:
            msg = "No ROI found for ROI-only corrected zarr."
            print(f"[Step1] {msg}")
            if not auto:
                QMessageBox.warning(self, "Step1", msg)
            return False

        patches = [] if handoff_schema >= 2 else list(self.step0_output.get("patches") or self._all_patches or [])
        if os.path.exists(patch_path):
            try:
                with open(patch_path, "r", encoding="utf-8") as f:
                    patch_cfg = json.load(f)
                if handoff_schema >= 2 and not isinstance(patch_cfg, list):
                    raise ValueError("patch config root must be an array")
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
                if handoff_schema >= 2:
                    return False
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
            try:
                channels = self.loader.channel_names()
            except Exception as e:
                print(f"[Step1] loader channel list failed: {e}")
                if handoff_schema >= 2:
                    return False
                channels = []
            nucleus_channel = self._choose_step1_nucleus_channel(
                channels,
                manifest=manifest,
                correction_config=correction_config,
            )
            panel_groups = (manifest.get("panel_groups") if handoff_schema >= 2 else self.step0_output.get("panel_groups")) or {}
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
            self.config.set_channels(channels)
            self.config.load_panel(panel_groups, nucleus_channel)
            self.config.set_nucleus(nucleus_channel, 1.0)   # Step0's answer
            # `zero_marker_weights()` is NOT called here any more, and that
            # is the whole of this fix. It is the Reset weights BUTTON --
            # a user saying "zero" -- and since the weights each marker has
            # been given are now remembered apart from the numbers, saying it
            # on every load marked every marker as already answered, so the
            # first tick of each kept 0 and the channel stayed invisible.
            # It was also redundant: `load_panel` above builds the new
            # dataset's markers at 0 in the rows AND in the groups (it enters
            # each channel at 0.0 whatever the handoff's panel_groups say),
            # unticked, and with no weight history. There is nothing left for
            # a reset to do except claim the zeros belong to somebody.
            print(f"[Step1] nucleus_channel={nucleus_channel}")
            print(f"[Step1] panel_groups source={source}")
            print(f"[Step1] config panel initialized={bool(self.config.get_groups())}")

        self._stop_all_loaders()
        self._patch_channel_cache.clear()
        self._overlay_display_cache.clear()
        self._signal_cache.clear()
        self._patch_load_ready.clear()
        self._preview_patch_idx = -1
        self._on_rois_changed(self._rois)
        self._on_patches(patches)
        # Keep the authoritative patch state populated even when a UI callback
        # is unavailable during restart/test construction.
        self._all_patches = list(patches or [])
        self._show_active_roi_preview()
        roi_id = manifest.get("roi_id") or (ctx or {}).get("roi_id", "")
        # A v2 manifest's step directories are authoritative even when an
        # older writer omitted roi_dir.  The resolver may synthesize a
        # project/ROI location in that case, which must not redirect the
        # handoff away from the manifest's own step0 directory.
        roi_dir = manifest.get("roi_dir") or ""
        if not roi_dir and handoff_schema >= 2:
            step0_parent = os.path.dirname(os.path.abspath(step0_dir))
            if os.path.basename(os.path.abspath(step0_dir)) == "step0":
                roi_dir = step0_parent
        if not roi_dir:
            roi_dir = (ctx or {}).get("roi_dir", "")
        project_dir = manifest.get("project_output_dir") or (ctx or {}).get("project_dir", "")
        if step1_dir:
            os.makedirs(step1_dir, exist_ok=True)
            OUTPUT_DIR = step1_dir
        gui_work_dir = step1_dir or out_dir
        self._set_gui_work_dir(gui_work_dir)
        if hasattr(self._step2, "set_roi_context"):
            self._step2.set_roi_context(roi_id=roi_id, roi_dir=roi_dir, step2_dir=step2_dir)
        # The geometry baseline this manifest publishes. Step0's counter is
        # per PAGE and starts at zero, so a page that comes up on an existing
        # project would otherwise number its first patch edit over a file this
        # manifest is pointing at. (The writer takes the baseline from disk
        # too; this keeps the page's own number honest from the moment it is
        # bound to a handoff.)
        adopt = getattr(self.__dict__.get("_step0"),
                        "adopt_published_geometry_revision", None)
        if adopt is not None:
            adopt(manifest)
        self.step0_output.update({
            "output_dir": gui_work_dir,
            "project_output_dir": project_dir,
            "roi_id": roi_id,
            "roi_dir": roi_dir,
            "step0_dir": step0_dir,
            "step1_dir": step1_dir,
            "step2_dir": step2_dir,
            "step0_manifest_path": manifest_path,
            "correction_config_path": cfg_path,
            "roi_config_path": roi_path,
            "patch_config_path": patch_path,
            "corrected_zarr_path": corr_path,
            "channel_remap_config_path": _path(
                manifest.get("channel_remap_config_path", self.step0_output.get("channel_remap_config_path", "")),
                manifest_base),
            "channel_remap_config_hash": manifest.get("channel_remap_config_hash", self.step0_output.get("channel_remap_config_hash", "")),
            "source_identity": manifest.get("source_identity", self.step0_output.get("source_identity")),
            "handoff_schema_version": manifest.get("handoff_schema_version", handoff_schema),
            "panel_groups": (manifest.get("panel_groups") if handoff_schema >= 2 else self.step0_output.get("panel_groups")) or {},
            "panel_nucleus": manifest.get("panel_nucleus", self.step0_output.get("panel_nucleus")),
            "rois": self._rois,
            "patches": list(patches or []),
        })
        print("[Step1] preloading patches...")
        if not auto:
            self.prev_status.setText("Step0 ROI result loaded.")
        self._log_step1_layout("after loading ROI sizes")
        self._schedule_step1_session_save()
        # Manual Load Step0 uses this same reader as the signal path.  Only a
        # complete successful read establishes Step0/Step1 readiness.
        self.step0_done = True
        self._step1_context_ready = True
        return True

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
                ctx = resolve_roi_context(p, p)
                step0_dir = (ctx or {}).get("step_dirs", {}).get("step0", p)
                if (
                    os.path.exists(os.path.join(step0_dir, "step0_roi_result.json"))
                    or os.path.exists(os.path.join(step0_dir, "corrected_channels.zarr"))
                    or os.path.exists(os.path.join(step0_dir, "roi_config.json"))
                ):
                    out_dir = step0_dir
                    break
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

        ctx = resolve_roi_context(out_dir, OUTPUT_DIR)
        step0_dir = (ctx or {}).get("step_dirs", {}).get("step0", out_dir)
        step1_dir = (ctx or {}).get("step_dirs", {}).get("step1", out_dir)
        step2_dir = (ctx or {}).get("step_dirs", {}).get("step2", out_dir)
        manifest = {}
        manifest_path = (ctx or {}).get("step0_manifest_path") or os.path.join(step0_dir, "step0_roi_result.json")
        manifest_exists = os.path.exists(manifest_path)
        if manifest_exists:
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                step0_dir = manifest.get("step0_dir") or manifest.get("output_dir") or step0_dir
                step1_dir = manifest.get("step1_dir") or (
                    os.path.join(manifest["roi_dir"], "step1")
                    if manifest.get("roi_dir") else step1_dir
                )
                step2_dir = manifest.get("step2_dir") or (
                    os.path.join(manifest["roi_dir"], "step2")
                    if manifest.get("roi_dir") else step2_dir
                )
                # The selected/context-resolved file is the manifest being
                # read.  A self-reported path is metadata, never a redirect
                # into another ROI/session.
                manifest_path = os.path.abspath(manifest_path)
                out_dir = step0_dir
            except Exception as e:
                print(f"[Step1] failed to load step0_roi_result.json during bootstrap: {e}")
                return False

        try:
            manifest_schema = int(manifest.get("handoff_schema_version", 1) or 1)
        except (TypeError, ValueError):
            print("[Step1] invalid handoff schema in bootstrap manifest")
            return False
        ome_candidates = []
        for p in (
            manifest.get("raw_ome_path"),
            self._ome_path_edit.text().strip() if hasattr(self, "_ome_path_edit") else "",
            OME_TIFF_FILE,
        ):
            if p and p not in ome_candidates:
                ome_candidates.append(p)
        parent = os.path.dirname(out_dir)
        if manifest_schema < 2 and parent and os.path.isdir(parent):
            ome_candidates.extend(glob.glob(os.path.join(parent, "*.ome.tif")))
            ome_candidates.extend(glob.glob(os.path.join(parent, "*.ome.tiff")))

        ome_path = next((p for p in ome_candidates if p and os.path.exists(p)), "")
        if manifest_schema >= 2 and (not manifest.get("raw_ome_path") or not ome_path):
            print("[Step1] authoritative manifest has no usable raw OME path")
            return False
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

        OUTPUT_DIR = step1_dir or out_dir
        OME_TIFF_FILE = ome_path
        gui_work_dir = step1_dir or out_dir
        self._set_gui_work_dir(gui_work_dir)
        self.step0_output = {
            "output_dir": gui_work_dir,
            "project_output_dir": manifest.get("project_output_dir") or (ctx or {}).get("project_dir", ""),
            "roi_id": manifest.get("roi_id") or (ctx or {}).get("roi_id", ""),
            "roi_dir": manifest.get("roi_dir") or (ctx or {}).get("roi_dir", ""),
            "step0_dir": step0_dir,
            "step1_dir": step1_dir,
            "step2_dir": step2_dir,
            "step0_manifest_path": manifest_path if manifest_exists else "",
            "ome_tiff_path": os.path.abspath(ome_path) if ome_path else "",
            "loader": self.loader,
            "corrected_zarr_path": manifest.get("corrected_zarr_path") or os.path.join(step0_dir, "corrected_channels.zarr"),
            "handoff_schema_version": manifest_schema,
            "source_identity": manifest.get("source_identity"),
            "channel_remap_config_path": manifest.get("channel_remap_config_path", ""),
            "channel_remap_config_hash": manifest.get("channel_remap_config_hash", ""),
            "panel_csv_path": manifest.get("panel_csv_path", ""),
            "panel_groups": manifest.get("panel_groups") or {},
            "panel_nucleus": manifest.get("panel_nucleus"),
        }
        # Bootstrap only supplies loader/manifest hints.  Step0 is not done
        # until the authoritative reader accepts every required artifact.
        self.step0_done = False
        self._step1_context_ready = False
        self._step2._out_edit.setText(step2_dir or out_dir)
        self._step4._ome_edit.setText(ome_path)
        self._step4._out_edit.setText(step2_dir or out_dir)
        print(f"[Step1] bootstrapped from disk output_dir={step0_dir}")
        print(f"[Step1] raw_ome={ome_path}")
        if not os.path.exists(os.path.join(step0_dir, "patch_config.json")):
            print("[Step1] patch_config.json not found; previous patches cannot be restored from Step0 output.")
        return True

    def _step1_session_path(self, output_dir=None):
        base = output_dir or (self.step0_output or {}).get("output_dir") or OUTPUT_DIR
        return os.path.join(base, "step1_session.json")

    def _find_step1_session(self):
        """Find only the session belonging to the current Step0 handoff.

        Automatic restore must never choose a sibling ROI merely because its
        session is newer.  Explicit ``path=`` loads remain the user's manual
        Browse escape hatch.
        """
        handoff = self.step0_output or {}
        step1_dir = handoff.get("step1_dir")
        if not step1_dir:
            return ""
        candidate = os.path.join(os.path.abspath(step1_dir), "step1_session.json")
        if not os.path.exists(candidate):
            return ""
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                sess = json.load(f)
        except (OSError, ValueError, TypeError):
            return ""
        if not isinstance(sess, dict):
            return ""
        expected_manifest_raw = handoff.get("step0_manifest_path") or ""
        expected_manifest = os.path.abspath(expected_manifest_raw) if expected_manifest_raw else ""
        try:
            handoff_schema = int(handoff.get("handoff_schema_version", 1) or 1)
        except (TypeError, ValueError):
            return ""
        if handoff_schema >= 2:
            # A v2 session is restorable only when the current handoff itself
            # is fully bound.  Never treat an absent manifest/identity as a
            # wildcard that can match a sibling session.
            if not expected_manifest or not isinstance(
                    handoff.get("source_identity"), dict) or not handoff.get("source_identity"):
                return ""
            actual_manifest_raw = sess.get("step0_manifest_path") or ""
            actual_manifest = os.path.abspath(actual_manifest_raw) if actual_manifest_raw else ""
            if not actual_manifest or actual_manifest != expected_manifest:
                return ""
            expected_identity = handoff.get("source_identity") or {}
            actual_identity = sess.get("source_identity") or {}
            if not isinstance(actual_identity, dict) or not actual_identity:
                return ""
            if actual_identity != expected_identity:
                return ""
        return candidate

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
        for gdata in fusion_cfg.get("groups", {}).values():
            for ch, weight in (gdata.get("channels") or {}).items():
                channel_weights[ch] = float(weight)
        nuc = fusion_cfg.get("nucleus") or {}
        if nuc.get("channel"):
            channel_weights[nuc["channel"]] = float(nuc.get("weight", 0.0))
        # Display state, Step1's own.  `channel_visibility` used to be a shadow
        # of "weight > 0" that nothing read; it now carries the real ticks.
        visible = set(self.config.visible_channels())
        channel_visibility = {ch: (ch in visible)
                              for ch in (self.config.all_channels or [])}

        return {
            "version": 1,
            "mode": "roi" if active_roi else "full_wsi",
            "roi_id": (self.step0_output or {}).get("roi_id", ""),
            "roi_dir": (self.step0_output or {}).get("roi_dir", ""),
            "step0_dir": (self.step0_output or {}).get("step0_dir", ""),
            "step1_dir": (self.step0_output or {}).get("step1_dir", out_dir),
            "step2_dir": (self.step0_output or {}).get("step2_dir", ""),
            "active_roi": active_roi.get("name", "ROI_1") if active_roi else "",
            "roi_bbox": roi_bbox,
            "rois": self._rois,
            "patches": patches,
            "selected_patch": f"P{self._preview_patch_idx+1}" if 0 <= self._preview_patch_idx < len(self._all_patches) else "",
            "corrected_zarr_path": self._corrected_zarr_path or os.path.join(out_dir, "corrected_channels.zarr"),
            "corrected_zarr_mode": self._corrected_zarr_mode,
            "fusion_zarr_path": self._fused_zarr_path or (self.step1_output or {}).get("zarr_path", ""),
            "raw_ome_path": getattr(self.loader, "filepath", OME_TIFF_FILE),
            "source_identity": (self.step0_output or {}).get("source_identity"),
            "step0_manifest_path": (self.step0_output or {}).get("step0_manifest_path", ""),
            "handoff_schema_version": (self.step0_output or {}).get("handoff_schema_version", 1),
            "output_dir": out_dir,
            "fusion_config": fusion_cfg,
            "channel_weights": channel_weights,
            "channel_visibility": channel_visibility,
            # Which weights are ANSWERS rather than absences. Without it a
            # restored session cannot tell a marker nobody ever enabled from
            # one the user deliberately set to 0.00 -- both are 0.0 in
            # `channel_weights` -- and the next first tick would guess.
            "channel_weight_initialized":
                self.config.weight_initialized_channels(),
            "channel_colors": self.config.channel_colors(),
            "current_channel": self.config.current_channel(),
            "preview_mode": self._step1_preview_mode,
            "p2_params": self._p2_params,
            "segmentation_preview_history": self._seg_preview_history,
            "active_segmentation_method": self._active_segmentation_method,
            "active_preview_patch": self._active_preview_patch,
            "p1_diam": self._p1_diam,
            "params_source": self._params_source,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _json_safe_step1_value(value):
        try:
            return json.loads(json.dumps(value, ensure_ascii=False))
        except Exception:
            if isinstance(value, dict):
                return {str(k): MainWindow._json_safe_step1_value(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [MainWindow._json_safe_step1_value(v) for v in value]
            try:
                return value.item()
            except Exception:
                return str(value)

    def _record_segmentation_preview_result(self, patch_idx, params, masks):
        params = normalize_segmentation_config(params or {})
        method = params.get("method") or CELLPOSE_WHOLECELL_FUSION
        patch_name = f"P{int(patch_idx) + 1}"
        cells = int(np.asarray(masks).max()) if masks is not None else 0
        device = str(params.get("device_used") or params.get("device") or "unknown")
        record = {
            "params": self._json_safe_step1_value(params),
            "cells": cells,
            "device": device,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        patch_hist = self._seg_preview_history.setdefault(patch_name, {})
        method_hist = patch_hist.setdefault(method, {})
        history = method_hist.setdefault("history", [])
        history.append(record)
        method_hist["latest"] = record

        is_active_params = params.get("_phase") != 1
        if is_active_params:
            self._p2_params = params
            self._active_segmentation_method = method
            self._active_preview_patch = patch_name
        return is_active_params

    def _handle_patch_segmentation_result(self, item):
        patch_idx = int(item.get("patch_idx", 0) or 0)
        params = normalize_segmentation_config(item.get("params") or {})
        phase = int(params.get("_phase") or item.get("phase") or 0)
        phase_key = "phase2" if phase == 2 else "phase1" if phase == 1 else "preview"
        masks = item.get("masks")
        mask_arr = None if masks is None else np.asarray(masks, dtype=np.uint32)
        labels = int(mask_arr.max()) if mask_arr is not None and mask_arr.size else 0
        bbox = tuple(int(v) for v in self._all_patches[patch_idx]) if 0 <= patch_idx < len(self._all_patches) else ()
        result = {
            "patch_id": f"P{patch_idx + 1}",
            "patch_idx": patch_idx,
            "bbox_global": list(bbox),
            "bbox_fullres": list(bbox),
            "local_mask": mask_arr,
            "preview_image_shape": list(mask_arr.shape) if mask_arr is not None else [],
            "params_used": self._json_safe_step1_value(params),
            "method": params.get("method", CELLPOSE_WHOLECELL_FUSION),
            "phase": f"phase{phase}" if phase else "",
            "result_path": item.get("result_path", ""),
            "labels_count": labels,
            "success": bool(item.get("success", True)),
        }
        rec = self._patch_seg_results.setdefault(patch_idx, {})
        print(f"[Step1-CellposeResult] received phase={phase}")
        target_viewer = "phase2" if phase == 2 else "phase1" if phase == 1 else "preview"
        print(f"[Step1-CellposeResult] dispatch to {target_viewer} viewer")
        print(f"[Step1-Phase2] result received patch_idx={patch_idx}" if phase == 2 else f"[Step1-Phase1] result received patch_idx={patch_idx}")
        print(f"[Step1-Phase2] result phase={phase}" if phase == 2 else f"[Step1-Phase1] result phase={phase}")
        print(f"[Step1-Phase2] masks shape={None if mask_arr is None else mask_arr.shape}" if phase == 2 else f"[Step1-Phase1] masks shape={None if mask_arr is None else mask_arr.shape}")
        print(f"[Step1-Phase2] labels count={labels}" if phase == 2 else f"[Step1-Phase1] labels count={labels}")
        if phase == 2:
            if bool(item.get("success", labels > 0)):
                rec[phase_key] = result
                print("[Step1-Phase2] cached as phase2")
            else:
                print(f"[Step1-UI] phase2 empty; keeping phase1 overlay patch_id=P{patch_idx + 1}")
        else:
            rec[phase_key] = result
        if phase == 1:
            print(f"[Step1-Phase1Viewer] mask labels={labels}")
        elif phase == 2:
            print(f"[Step1-Phase2Viewer] mask labels={labels}")
        updating = patch_idx == self._preview_patch_idx
        if phase == 2:
            print(f"[Step1-Phase2] current_patch_idx={self._preview_patch_idx}")
            print("[Step1-Phase2] updating overlay now=False")
        print("[Step1-FusionPreview] no mask overlay rendered")

    def _update_patch_phase_result(self, item):
        self._handle_patch_segmentation_result(item)

    def _cellpose_result_rgb_for_grid(self, item):
        rgb = item.get("rgb_raw")
        if rgb is not None:
            return rgb
        result_path = item.get("result_path") or ""
        if result_path and os.path.exists(result_path):
            try:
                with np.load(result_path, allow_pickle=False) as data:
                    if "rgb_raw" in data:
                        rgb = data["rgb_raw"]
                        print(f"[Phase2-debug] loaded rgb_raw fallback from result_path={result_path}")
                        return rgb
            except Exception:
                print(f"[Phase2-debug] failed to load rgb fallback result_path={result_path}")
        masks = item.get("masks")
        if masks is not None:
            print("[Phase2-debug] using mask label image fallback")
            mask_arr = np.asarray(masks, dtype=np.uint32)
            n = int(mask_arr.max()) if mask_arr.size else 0
            if n > 0:
                rng = np.random.default_rng(12345)
                lut = rng.integers(40, 255, size=(n + 1, 3), dtype=np.uint8)
                lut[0] = 0
                return lut[np.clip(mask_arr, 0, n).astype(np.int32)]
            return np.zeros((*mask_arr.shape, 3), dtype=np.uint8)
        return None

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
        """Restore the fusion config into the one channel panel.

        Group membership and group weights are restored as they were saved:
        the panel no longer SHOWS groups, but it still carries them, because a
        folded weight would change old projects and everything downstream.
        """
        cfg = dict(cfg or {})
        names = self.loader.channel_names() if self.loader else []
        self.config.set_channels(names)
        self.config.apply_full_config(cfg)

    def _restore_step1_weight_history(self, sess):
        """Put back which weights are answers rather than absences.

        Presence of the KEY is the test, not its truth: an empty list is a
        session in which nobody had weighted anything yet, and reading that as
        "absent" would let the next first tick leave the channel invisible at
        0. A session written before this field existed falls back to the
        conservative migration `apply_full_config` has already performed --
        every weight in the saved fusion config treated as authoritative, 0
        included -- so opening an old project and ticking a channel cannot
        silently rewrite a weight it had stored.

        Called from BOTH restore paths. The v2 path reaches it through the
        display state; the older path restores no display state at all, but it
        still loads a session this program may have written, and that session
        knows which of its zeros nobody ever chose.
        """
        sess = dict(sess or {})
        if "channel_weight_initialized" not in sess:
            return
        recorded = sess.get("channel_weight_initialized")
        if isinstance(recorded, (list, tuple, dict)):
            self.config.restore_weight_initialization(recorded)

    def _apply_step1_display_state(self, sess):
        """Restore Step1's own display state: ticks, colours, current channel,
        preview mode.

        Restoring is not clicking.  It goes through the panel's batch entry so
        a channel the user deliberately left hidden comes back hidden even when
        it is the current one, and so the preview is drawn once at the end
        rather than once per channel.

        None of this belongs to the Step0 handoff — it never touches the remap
        config the manifest hashes — so it is restored here, after the
        authoritative reader has settled geometry and data sources.  A dataset
        with no session simply keeps the defaults.
        """
        sess = dict(sess or {})
        # Everything is put back silently first — the mode included, because
        # which channels the restored ticks require depends on it — and then
        # reconciled once, so a restore is one load and one redraw rather than
        # a chain of them.
        mode = str(sess.get("preview_mode") or "")
        if mode in (STEP1_PREVIEW_OVERLAY, STEP1_PREVIEW_FUSION):
            self.set_preview_mode(mode, force=True, reconcile=False)

        self._restore_step1_weight_history(sess)

        colors = sess.get("channel_colors")
        visibility = sess.get("channel_visibility")
        current = str(sess.get("current_channel") or "")
        if colors or visibility or current:
            self._restoring_display_state = True
            try:
                self.config.restore_display_state(
                    colors=colors if isinstance(colors, dict) else None,
                    visibility=visibility if isinstance(visibility, dict) else None,
                    current_channel=current)
            finally:
                self._restoring_display_state = False
            self._on_display_state_restored()
        else:
            self._on_display_state_restored()

    def _on_display_state_restored(self):
        """One load and one redraw after a bulk restore."""
        self._restore_fusion_settings()
        self._ensure_channels_cached(self._preview_patch_idx)
        self._refresh_patch_preview(reset_view=False)
        self._schedule_step1_session_save()

    def _apply_step1_session_fields(self, sess, out_dir, raw_ome, roi_dir,
                                    step2_dir, roi_id=""):
        """Apply fields owned by Step1 after the Step0 handoff is loaded.

        Geometry and data sources have already been populated by
        :meth:`_load_step0_roi_result`.  This helper never reads session ROI,
        patch, raw, corrected, or workspace path fields.
        """
        # v2 geometry/data is authoritative in the manifest reader.  Do not
        # call _on_patches with session data or reconstruct an ROI.
        patches = list(self._all_patches or [])

        fusion_cfg = sess.get("fusion_config") or self._fusion_config_from_flat_weights(sess)
        self._apply_step1_fusion_config(fusion_cfg)
        self._apply_step1_display_state(sess)

        self._p2_params = sess.get("p2_params")
        if self._p2_params and hasattr(getattr(self, "search", None), "apply_seg_config_to_ui"):
            self.search.apply_seg_config_to_ui(self._p2_params)
        self._seg_preview_history = dict(sess.get("segmentation_preview_history") or {})
        self._active_segmentation_method = str(
            sess.get("active_segmentation_method")
            or (self._p2_params or {}).get("method")
            or ""
        )
        self._active_preview_patch = str(sess.get("active_preview_patch") or "")
        self._p1_diam = sess.get("p1_diam")
        self._params_source = sess.get("params_source")
        self._fused_zarr_path = self._restorable_fused_zarr(
            sess.get("fusion_zarr_path")) or None

        self.step1_output = {
            "fusion_config_path": os.path.join(out_dir, "fusion_config.json"),
            "correction_config_path": self.step0_output.get("correction_config_path") or
                                      os.path.join(out_dir, "correction_config.json"),
            "zarr_path": self._fused_zarr_path,
            "roi_info": self._rois,
            "output_dir": out_dir,
            "step2_dir": step2_dir,
            "roi_id": roi_id,
            "roi_dir": roi_dir,
            "ome_tiff_path": raw_ome,
        }

        selected_name = sess.get("selected_patch")
        if selected_name and selected_name.startswith("P"):
            try:
                sel_idx = int(selected_name[1:]) - 1
                if 0 <= sel_idx < len(self._all_patches):
                    self._select_preview_patch(sel_idx)
            except Exception:
                pass
        self.step1_done = bool(self._fused_zarr_path)
        if hasattr(self._step2, "set_roi_context"):
            self._step2.set_roi_context(
                roi_id=roi_id,
                roi_dir=roi_dir,
                step2_dir=step2_dir,
            )
        else:
            self._step2._out_edit.setText(step2_dir or out_dir)
        self._step4._ome_edit.setText(raw_ome)
        self._step4._out_edit.setText(step2_dir or out_dir)
        self._update_next_button()
        self.prev_status.setText("Loaded previous Step1 session.")
        print(f"[Step1] restored patches={len(patches)}")
        print(f"[Step1] restored channel weights count={len(sess.get('channel_weights') or {})}")
        # Publish readiness only after the complete Step1 overlay has been
        # applied.  Any exception above therefore remains fail-closed.
        self.step0_done = True
        self._step1_context_ready = True
        return True

    def _restore_step1_session_v2(self, sess, manifest_path):
        """Restore Step1 state on top of the single authoritative v2 reader."""
        # A failed restore must never leave a previously accepted Step0/Step1
        # context usable.  The reader sets these flags only after its complete
        # handoff succeeds; keep them false while the session overlay is being
        # validated and applied.
        self.step0_done = False
        self._step1_context_ready = False
        if not manifest_path:
            print("[Step1] v2 session has no exact Step0 manifest")
            return False

        # Preserve and validate an already-bound handoff before replacing the
        # small payload used to invoke the authoritative reader.
        current_handoff = self.step0_output or {}
        expected_manifest = current_handoff.get("step0_manifest_path") or ""
        if expected_manifest and os.path.abspath(expected_manifest) != os.path.abspath(manifest_path):
            print("[Step1] session manifest does not match the current handoff")
            return False
        expected_identity = current_handoff.get("source_identity") or {}
        if expected_identity and sess.get("source_identity") != expected_identity:
            print("[Step1] session source identity does not match the current handoff")
            return False

        # The existing loader is only a hint.  The authoritative reader below
        # compares it with the manifest raw path and replaces it if needed.
        self.step0_output = {
            "step0_dir": os.path.dirname(manifest_path),
            "output_dir": os.path.dirname(manifest_path),
            "step0_manifest_path": manifest_path,
            "handoff_schema_version": 2,
        }
        if self._load_step0_roi_result(auto=True) is not True:
            return False

        # The authoritative reader marks the handoff ready on success.  The
        # session overlay is not complete yet, so clear the ready state again
        # until all Step1-owned fields have been restored successfully.
        self.step0_done = False
        self._step1_context_ready = False
        authority = self.step0_output
        if sess.get("source_identity") != authority.get("source_identity"):
            print("[Step1] session source identity does not match the Step0 authority")
            return False
        out_dir = authority.get("output_dir") or OUTPUT_DIR
        raw_ome = getattr(self.loader, "filepath", "") or authority.get("ome_tiff_path", "")
        roi_dir = authority.get("roi_dir", "")
        step2_dir = authority.get("step2_dir", "")
        roi_id = authority.get("roi_id", "")
        return self._apply_step1_session_fields(
            sess, out_dir, raw_ome, roi_dir, step2_dir, roi_id=roi_id,
        )

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
            if not isinstance(sess, dict):
                raise ValueError("session root must be an object")

            session_manifest_raw = sess.get("step0_manifest_path") or ""
            try:
                session_schema_hint = int(sess.get("handoff_schema_version", 1) or 1)
            except (TypeError, ValueError):
                return False
            # A v2 session must invalidate any previous readiness before the
            # manifest binding checks below.  Leave the legacy compatibility
            # branch isolated from this state transition.
            if session_schema_hint >= 2:
                self.step0_done = False
                self._step1_context_ready = False
            # Read only the manifest schema header to choose the restore
            # protocol.  The v2 reader remains the sole owner of all manifest
            # validation and artifact loading.
            session_manifest_path = os.path.abspath(session_manifest_raw) if session_manifest_raw else ""
            manifest_schema = session_schema_hint
            manifest_header = {}
            if session_manifest_path:
                try:
                    with open(session_manifest_path, "r", encoding="utf-8") as f:
                        manifest_header = json.load(f)
                    if not isinstance(manifest_header, dict):
                        raise ValueError("manifest root must be an object")
                    manifest_schema = int(
                        manifest_header.get("handoff_schema_version", 1) or 1
                    )
                except Exception as e:
                    print(f"[Step1] failed to read session manifest header: {e}")
                    if session_schema_hint >= 2:
                        return False
                    manifest_schema = session_schema_hint
            if manifest_schema >= 2 and session_schema_hint < 2:
                # A legacy-looking session can still point at a v2 manifest;
                # fail closed before any v2 binding check in that case too.
                self.step0_done = False
                self._step1_context_ready = False
            if manifest_schema >= 2 and session_manifest_path:
                # This is only a session-to-manifest binding check.  Artifact
                # identity and all other validation remain in the authority
                # reader below.
                if sess.get("source_identity") != manifest_header.get("source_identity"):
                    print("[Step1] session source identity does not match the manifest header")
                    return False
            if session_schema_hint >= 2 and manifest_schema < 2:
                print("[Step1] v2 session hint cannot use a v1 manifest")
                return False
            # A manually browsed session is still not allowed to replace the
            # active Step0 handoff with a sibling ROI.  When a handoff is
            # already loaded, bind the session to its exact Step1 directory,
            # manifest, and source identity.
            current_handoff = self.step0_output or {}
            expected_step1_dir = current_handoff.get("step1_dir") or ""
            if expected_step1_dir:
                expected_session_dir = os.path.abspath(expected_step1_dir)
                if os.path.dirname(os.path.abspath(session_path)) != expected_session_dir:
                    print("[Step1] session is outside the current Step1 directory")
                    return False
            expected_manifest = current_handoff.get("step0_manifest_path") or ""
            actual_manifest = sess.get("step0_manifest_path") or ""
            if expected_manifest and (not actual_manifest or
                                      os.path.abspath(actual_manifest) != os.path.abspath(expected_manifest)):
                print("[Step1] session manifest does not match the current handoff")
                return False
            expected_identity = current_handoff.get("source_identity") or {}
            if expected_identity and sess.get("source_identity") != expected_identity:
                print("[Step1] session source identity does not match the current handoff")
                return False

            # Schema-v2 sessions are state overlays on the committed Step0
            # handoff.  The manifest reader owns raw/corrected/ROI/Patch,
            # remap, and all workspace directories; the session contributes
            # only Step1 UI/history state after that read succeeds.
            if manifest_schema >= 2:
                return self._restore_step1_session_v2(sess, session_manifest_path)

            session_dir = os.path.dirname(os.path.abspath(session_path))
            # Schema 1 sessions retain their historical self-contained
            # geometry/data-source compatibility behavior.
            out_dir = sess.get("output_dir") or session_dir
            raw_ome = sess.get("raw_ome_path") or OME_TIFF_FILE
            if not raw_ome or not os.path.exists(raw_ome):
                QMessageBox.warning(self, "Step1", "Raw OME-TIFF path missing. Please load Step0 or edit the session.")
                return False
            roi_dir = sess.get("roi_dir", "")
            step2_dir = (sess.get("step2_dir")
                         or (os.path.join(roi_dir, "step2") if roi_dir else ""))
            if not step2_dir and os.path.basename(os.path.abspath(out_dir)) == "step1":
                step2_dir = os.path.join(os.path.dirname(os.path.abspath(out_dir)), "step2")
            OUTPUT_DIR = out_dir
            OME_TIFF_FILE = raw_ome
            self._set_gui_work_dir(out_dir)
            self.step0_output = {
                "output_dir": out_dir,
                "ome_tiff_path": raw_ome,
                "roi_id": sess.get("roi_id", ""),
                "roi_dir": roi_dir,
                "step0_dir": sess.get("step0_dir", ""),
                "step1_dir": sess.get("step1_dir", out_dir),
                "step2_dir": step2_dir,
                "step0_manifest_path": (os.path.abspath(session_manifest_raw)
                                         if session_manifest_raw else ""),
                "handoff_schema_version": session_schema_hint,
                "source_identity": sess.get("source_identity"),
            }
            self.loader = OMETIFFLoader(raw_ome)

            corr_path = (sess.get("corrected_zarr_path")
                         or os.path.join(out_dir, "corrected_channels.zarr"))
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

            self.config.set_channels(self.loader.channel_names())
            fusion_cfg = sess.get("fusion_config") or self._fusion_config_from_flat_weights(sess)
            self._apply_step1_fusion_config(fusion_cfg)
            # This path restores no display state, but the weight history is
            # not display state: without it every 0 in the config counts as
            # somebody's answer, and a channel nobody ever enabled stays
            # invisible at 0 on its first tick after a restart.
            self._restore_step1_weight_history(sess)

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
            self._active_roi = next(
                (r for r in rois if r.get("name") == active_name),
                rois[0] if rois else None,
            )
            print(f"[Step1] active_roi={active_name or 'none'}")

            patches = []
            ry0, _, rx0, _ = [0, 0, 0, 0]
            if self._active_roi and self._active_roi.get("bbox_fullres"):
                ry0, _, rx0, _ = [int(v) for v in self._active_roi["bbox_fullres"]]
            for item in sess.get("patches") or []:
                if item.get("bbox_fullres"):
                    patches.append(tuple(int(v) for v in item["bbox_fullres"]))
                elif item.get("bbox_local"):
                    y0, y1, x0, x1 = [int(v) for v in item["bbox_local"]]
                    patches.append((ry0 + y0, ry0 + y1, rx0 + x0, rx0 + x1))
            self._p2_params = sess.get("p2_params")
            if self._p2_params and hasattr(self.search, "apply_seg_config_to_ui"):
                self.search.apply_seg_config_to_ui(self._p2_params)
            self._seg_preview_history = dict(sess.get("segmentation_preview_history") or {})
            self._active_segmentation_method = str(
                sess.get("active_segmentation_method")
                or (self._p2_params or {}).get("method")
                or ""
            )
            self._active_preview_patch = str(sess.get("active_preview_patch") or "")
            self._p1_diam = sess.get("p1_diam")
            self._params_source = sess.get("params_source")
            self._fused_zarr_path = self._restorable_fused_zarr(
                sess.get("fusion_zarr_path")) or None
            self.step1_output = {
                "fusion_config_path": os.path.join(out_dir, "fusion_config.json"),
                "correction_config_path": os.path.join(out_dir, "correction_config.json"),
                "zarr_path": self._fused_zarr_path,
                "roi_info": self._rois,
                "output_dir": out_dir,
                "step2_dir": step2_dir,
                "roi_id": sess.get("roi_id", ""),
                "roi_dir": roi_dir,
                "ome_tiff_path": raw_ome,
            }

            self._stop_all_loaders()
            self._patch_channel_cache.clear()
            self._overlay_display_cache.clear()
            self._signal_cache.clear()
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
            self._step1_context_ready = True
            self.step1_done = bool(self._fused_zarr_path)
            if hasattr(self._step2, "set_roi_context"):
                self._step2.set_roi_context(
                    roi_id=sess.get("roi_id", ""),
                    roi_dir=roi_dir,
                    step2_dir=step2_dir,
                )
            else:
                self._step2._out_edit.setText(step2_dir or out_dir)
            self._step4._ome_edit.setText(raw_ome)
            self._step4._out_edit.setText(step2_dir or out_dir)
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
        # Auto-sync Step2 Input Data from the finished Step1 whenever a Step1
        # result exists. NOT gated on is_sequential_flow: that flag is never set
        # True (the sequential "Next" button was removed), so gating here left the
        # handoff permanently dead — fused.zarr AND Segmentation Index never
        # populated for ANY segmentation method. Fill the two Input Data fields
        # only when still empty so a manual override / re-entry is never clobbered.
        if self.step1_output:
            step2_dir = self.step1_output.get("step2_dir") or (self.step0_output or {}).get("step2_dir")
            if step2_dir:
                os.makedirs(step2_dir, exist_ok=True)
            self._step2._out_edit.setText(
                step2_dir or self.step1_output.get("output_dir", OUTPUT_DIR)
            )
            zarr_path = self.step1_output.get("zarr_path")
            if zarr_path and not self._step2._zarr_edit.text().strip():
                self._step2.set_zarr_path(zarr_path)
            self._step2._fusion_config_path = self.step1_output.get("fusion_config_path")
            cfg_path = self.step1_output.get("fusion_config_path")
            out_dir = self.step1_output.get("output_dir", OUTPUT_DIR)
            try:
                if hasattr(self._step2, "load_step1_active_params") \
                        and not self._step2._seg_params_edit.text().strip():
                    self._step2.load_step1_active_params(out_dir)
            except Exception:
                print(f"[Step2] failed to load active segmentation params:\n{traceback.format_exc()}")
            if cfg_path:
                self._step2._zarr_info.setText(
                    (self._step2._zarr_info.text() or "") +
                    f"\nConfig: {os.path.basename(cfg_path)}"
                )
            rois = (self.step0_output or {}).get("rois") or self.step1_output.get("roi_info")
            if rois:
                self._step2.set_rois(rois)
            if hasattr(self._step2, "set_roi_context"):
                self._step2.set_roi_context(
                    roi_id=self.step1_output.get("roi_id") or (self.step0_output or {}).get("roi_id", ""),
                    roi_dir=self.step1_output.get("roi_dir") or (self.step0_output or {}).get("roi_dir", ""),
                    step2_dir=step2_dir or "",
                )
        self._stack.setCurrentIndex(2)
        self._set_step_active(2)

    def _go_to_step1(self):
        # Step1 reads the handoff from DISK. While a patch edit is still being
        # written, the newest geometry is in Step0's memory and one revision
        # ahead of the file, so entering now would run Step1 on the previous
        # geometry without saying so.
        # `self.__dict__`, not getattr: a QWidget whose __init__ never ran
        # raises on a missing attribute rather than returning the default,
        # and this method is driven that way by the handoff-contract tests.
        page = self.__dict__.get("_step0")
        ready = getattr(page, "geometry_ready_for_consumers", None)
        # NOT "is the worker busy": the page is told the outcome through a
        # queued signal, so there is a window in which the worker has finished
        # and nothing has been announced yet. The question is whether the file
        # on disk describes the geometry on screen -- which is false while a
        # write is in flight AND after one that failed.
        waiting = bool(ready is not None and not ready())
        blocked = getattr(page, "geometry_blocked_reason", None)
        blocked = blocked() if blocked is not None else None
        waits = int(self.__dict__.get("_step1_entry_waits") or 0)
        if waiting and blocked is None and waits < 40:
            # Bounded: 40 x 150 ms. A write that takes longer than six
            # seconds is reported, not waited on forever -- and it is still
            # refused, because the file Step1 would read is the old one.
            self._step1_entry_waits = waits + 1
            self.prev_status.setText(
                "Saving the patch geometry… Step1 opens once the new Step0 "
                "handoff is on disk.")
            QtCore.QTimer.singleShot(150, self._go_to_step1)
            return
        self._step1_entry_waits = 0
        if waiting:
            self.prev_status.setText(
                "Step1 is not ready: the patch geometry on disk is not the "
                "geometry on screen"
                + (f" ({blocked})." if blocked else
                   " — the write is still running."))
            return
        if not getattr(self, "_step1_context_ready", False):
            accepted = False
            handoff = self.step0_output or {}
            # A loader can survive a cancelled/partial load and must not make
            # Step1 guess which ROI/session it belongs to.  Only an explicitly
            # bound handoff (canonical manifest + step directories) may invoke
            # the authoritative Step0 reader here.  Do not auto-restore a
            # sibling/latest session from navigation.
            has_bound_handoff = bool(
                str(handoff.get("step0_manifest_path") or "").strip()
                and str(handoff.get("step0_dir") or "").strip()
                and str(handoff.get("step1_dir") or "").strip()
            )
            if has_bound_handoff:
                accepted = self._load_step0_roi_result(auto=True) is True
            if not accepted:
                self.prev_status.setText(
                    "Step1 is not ready: load a valid Step0 handoff or use Load Previous Step1 Session."
                )
                return
            self._step1_context_ready = True
        self._stack.setCurrentIndex(1)
        self._set_step_active(1)
        self._log_step1_layout("enter Step1")

    def _go_to_step1_5(self):
        """Open Step 1.5 (Background Correction + Channel Conditioning / Remap).

        Feeds the page from the current ROI/patch context (loader, step1_5 dir,
        patches, nucleus channel). Saves remap configs under
        <ROI>/step1_5/channel_remap_configs/. Does NOT auto-create any config.
        """
        if self.loader is None:
            QMessageBox.information(
                self, "Step 1.5",
                "Load a Step0/Step1 ROI first — the image loader is not initialized yet.")
            return
        ctx = self.step0_output or {}
        roi_dir = ctx.get("roi_dir", "")
        step1_5_dir = (os.path.join(roi_dir, "step1_5") if roi_dir
                       else (ctx.get("output_dir") or OUTPUT_DIR))
        patches = list(ctx.get("patches") or self._all_patches or [])
        nucleus_channel = ""
        if hasattr(self, "config"):
            nucleus_channel = self.config.nucleus_channel() or ""
        self._step1_5.set_context(self.loader, step1_5_dir, patches, nucleus_channel)
        self._stack.setCurrentWidget(self._step1_5)
        # Step1.5 is reached by widget and never updates `_current_step`, so the
        # navigator policy is set explicitly here: downstream of Step1, the
        # shared navigator is read-only.
        self._apply_navigator_policy_for_step(2)

    def _go_to_step3(self, output_dir=None):
        if self._current_step == 1:
            self._stop_all_loaders()
        # Breadcrumb navigation passes no output_dir; fall back to the completed
        # Step2 result so Step3 Input auto-syncs even when the user clicked OK on
        # the finish dialog (no auto-advance) and then clicked the Step3 tab.
        if not output_dir:
            output_dir = (self.step2_output or {}).get("output_dir")
        if output_dir:
            self._on_step2_complete(output_dir)
            self._step3.set_channel_context(
                loader=self.loader,
                corrected_zarr_path=self._corrected_zarr_path,
                rois=self._rois,
            )
            self._step3.set_output_dir(output_dir)
            self.step3_output = dict(self.step2_output)
        self._stack.setCurrentIndex(3)
        self._set_step_active(3)

    def _on_step2_complete(self, output_dir):
        self.step2_done = True
        self.step2_output = {
            "output_dir": output_dir,
        }
        if hasattr(self, "_step3"):
            self._step3.set_channel_context(
                loader=self.loader,
                corrected_zarr_path=self._corrected_zarr_path,
                rois=self._rois,
            )
        self._update_next_button()
        print(f"[MainWindow] Step2 complete output_dir={output_dir}")
        print(f"[MainWindow] step2_done={self.step2_done}")
        print(f"[MainWindow] next_enabled={self._btn_next.isEnabled()}")

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

    # (#11) _go_next_step removed — the "Next" button it served is gone; step
    # navigation is via the top-nav step names (_go_to_stepN).

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

    def _apply_navigator_policy_for_step(self, step):
        """Give the shared navigator the rights the CURRENT step has.

        Step0 defines the analysis region and edits it freely.  Step1 may drop an
        ROI (which invalidates the handoff, deliberately) and may edit patches.
        Every step downstream of Step1 consumes geometry it did not produce, so
        the navigator there is a map: no ROI edit, no patch edit.
        """
        if step == 0:
            roi_policy, patch_editable = "full", True
        elif step == 1:
            roi_policy, patch_editable = "delete_only", True
        else:
            roi_policy, patch_editable = "read_only", False
        # Through Block01's display layer, which HOLDS the policy: it outlives
        # the popup being opened or closed, and it is applied to the widgets
        # by whichever page furnished them.
        self._display.set_navigator_policy(roi_policy=roi_policy,
                                           patch_editable=patch_editable)

    def _return_to_step0(self, reason):
        """Send the user back to Step0 from wherever they are.

        Used when the handoff every downstream step depends on has stopped
        being valid.  Checked by widget, not by `_current_step`, because Step1.5
        is reached with `setCurrentWidget` and never updates that counter.
        """
        if self._stack.currentWidget() is self._step0:
            self._apply_navigator_policy_for_step(0)
            return False
        print(f"[Step1] returning to Step0: {reason}")
        self._go_to_step0()
        return True

    _STEP_CONTEXTS = {0: _CTX_STEP0, 1: _CTX_STEP1, 2: _CTX_STEP2,
                      3: _CTX_STEP3}

    def _set_step_active(self, active):
        self._current_step = active
        # The ONE place the shared navigator's edit policy is decided.  Every
        # navigation path goes through here, so an already-open popup follows
        # the step immediately: no reopen, no extra click.
        self._apply_navigator_policy_for_step(active)
        # ...and the ONE place the Tissue Preview's render context changes.
        # An atomic transition: it bumps Block01's generation, so every queued
        # timer, pending revision and worker result belonging to the step
        # being left is structurally stale from this line on -- and then asks
        # the step being entered for its first frame, so the popup shows the
        # new step rather than the old one's last picture.
        # A step downstream of Step1 draws the ONE published spec -- so it
        # can be composed again when a colour, a display window or a weight
        # moves, and so a weight moved there is still that weight in Step3
        # and back in Step1. Nothing is copied at the transition; copying is
        # what made a downstream edit disappear on the next step change.
        self._display.coordinator.set_active_context(
            self._STEP_CONTEXTS.get(active))
        # ...and the Intensity window's rights, from the same transition and
        # by the same rule: one window, one set of numbers, and what changes
        # with the step is only what may be edited.
        self._display.apply_intensity_policy()
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

    def _show_active_roi_preview(self):
        """Report the ROI/patch context Step1 is bound to.

        Step1 no longer draws its own tissue thumbnail: ROI and patches are
        viewed and edited in the one shared Tissue Preview, which Block01
        owns and every step shares.
        This is a status line over the authoritative geometry, nothing else.
        """
        roi = self._active_roi
        if self.loader is None:
            self.roi_status.setText("No ROI loaded")
            return
        if not roi:
            self.roi_status.setText(
                f"Full WSI  patches={len(self._all_patches)}  "
                "— open the Tissue Preview to view or edit them")
            return
        bbox = roi.get("bbox_fullres")
        if not bbox or len(bbox) != 4:
            self.roi_status.setText("No ROI bbox")
            return
        ry0, ry1, rx0, rx1 = [int(v) for v in bbox]
        self.roi_status.setText(
            f"{roi.get('name', 'ROI_1')}  {ry1 - ry0}×{rx1 - rx0}px  "
            f"patches={len(self._all_patches)}  "
            "— open the Tissue Preview to view or edit them")

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
        self._show_active_roi_preview()

        if not patches:
            self._stop_all_loaders()
            self._patch_channel_cache.clear()
            self._overlay_display_cache.clear()
            self._signal_cache.clear()
            self._patch_load_ready.clear()
            self._preview_patch_idx = -1
            self._selected_step1_patch_idx = -1
            self.prev_img.clear()
            self.prev_status.setText("No patch selected")
            self.patch_cache_status.setText(" ")
            self._schedule_step1_session_save()
            return

        # Drop cache for patches whose ROI coordinates changed
        if len(patches) < len(old_rois):
            self._patch_channel_cache.clear()
            self._overlay_display_cache.clear()
            self._signal_cache.clear()
            self._patch_load_ready.clear()
            self._patch_seg_results = {
                i: self._patch_seg_results.get(i, {})
                for i in range(len(patches))
            }
        if len(patches) != len(old_rois):
            # Patch names are positional ("P3"), so adding or removing one
            # renumbers the rest: every name-keyed result now points at a
            # different rectangle and none of it may be shown as current.
            self._seg_preview_history = {}
            self._active_preview_patch = ""
            self._preserve_view_after_patch_load.clear()
        for idx, roi in enumerate(patches):
            if idx < len(old_rois) and roi != old_rois[idx]:
                self._patch_channel_cache.pop(idx, None)
                self._drop_overlay_cache_for(idx)
                self._patch_load_ready.discard(idx)
                self._stop_loader_for(idx)
                self._patch_seg_results.pop(idx, None)
                self._preserve_view_after_patch_load.pop(idx, None)
                # Same rectangle name, different rectangle: its previews and
                # segmentation history describe pixels that are no longer there.
                self._seg_preview_history.pop(f"P{idx+1}", None)
                if self._active_preview_patch == f"P{idx+1}":
                    self._active_preview_patch = ""

        # Auto-select only newly added patches; keep selection on move/resize.
        if len(patches) > len(old_rois):
            new_idx = len(patches) - 1
        elif 0 <= self._preview_patch_idx < len(patches):
            new_idx = self._preview_patch_idx
        else:
            new_idx = min(self._selected_step1_patch_idx, len(patches) - 1)
            if new_idx < 0:
                new_idx = len(patches) - 1
        if new_idx != self._preview_patch_idx:
            self._preview_patch_idx = new_idx
            self._selected_step1_patch_idx = new_idx
            for i, btn in enumerate(self._patch_sel_btns):
                btn.setChecked(i == new_idx)

        # Show cached render instantly if available; otherwise wait for preload
        if self._preview_patch_idx in self._patch_load_ready:
            self._refresh_patch_preview(reset_view=True)
            self.patch_cache_status.setText(
                f"P{self._preview_patch_idx+1} (cached) — "
                f"preloading remaining patches in background…"
            )
        else:
            self.prev_img.clear()
            self.patch_cache_status.setText("Loading patch...")

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
        self._selected_step1_patch_idx = idx
        # Selecting a patch switches the preview only.  Geometry belongs to the
        # shared navigator and to Step0's published handoff.
        self._schedule_step1_session_save()

        if idx in self._patch_load_ready:
            # Only the channels this patch does not have yet are read, and the
            # ones it does have stay: a missing channel is not a reason to drop
            # everything already in memory.
            self._ensure_channels_cached(idx)
            self._refresh_patch_preview(reset_view=True)
        elif idx in self._patch_loaders:
            self.prev_img.clear()
            self.patch_cache_status.setText("Loading patch...")
        else:
            # Not yet started — kick off a single loader for this patch immediately
            self.prev_img.clear()
            self.patch_cache_status.setText("Loading patch...")
            self._start_loader_for(idx)

    # ── Background preloading ────────────────────────────────────────

    def _fusion_weighted_channels(self):
        """Channels the FUSION preview would actually use: the nucleus, and
        every grouped channel whose effective weight is above zero."""
        if self.loader is None:
            return []
        try:
            available = set(self.loader.channel_names())
        except Exception:
            return []
        wanted = []
        effective = self._effective_fusion_config()
        groups = {name: dict(data.get("channels") or {})
                  for name, data in (effective.get("groups") or {}).items()}
        group_weights = {name: float(data.get("group_weight", 1.0) or 0.0)
                         for name, data in (effective.get("groups") or {}).items()}
        for gname, ch_weights in groups.items():
            if float(group_weights.get(gname, 1.0) or 0.0) <= 0:
                continue
            for ch, weight in ch_weights.items():
                if ch in available and float(weight or 0) > 0 and ch not in wanted:
                    wanted.append(ch)
        nuc = effective.get("nucleus") or {}
        nuc_ch = nuc.get("channel", "")
        if (nuc_ch in available and float(nuc.get("weight", 0.0) or 0.0) > 0
                and nuc_ch not in wanted):
            wanted.append(nuc_ch)
        return wanted

    def _needed_channels(self):
        """The channels Step1 must hold pixels for, for the preview it is
        showing.

        Overlay draws what is ticked, so that is its set.  Fusion draws what it
        weighs, so its set is the nucleus plus every channel with a live
        weight.  The channel being edited is in both, because Intensity has to
        have something to work on.  What is never in either is "everything":
        a weight edit may pull in one channel that has just become necessary,
        and must never re-read the ones already in hand.
        """
        if self.loader is None:
            return []
        try:
            available = set(self.loader.channel_names())
        except Exception:
            return []
        if self._step1_preview_mode == STEP1_PREVIEW_FUSION:
            needed = [ch for ch in self._fusion_weighted_channels()]
        else:
            needed = [ch for ch in self.config.visible_channels()
                      if ch in available]
        current = self.config.current_channel()
        if current and current in available and current not in needed:
            needed.append(current)
        return needed

    @staticmethod
    def _limited_patch_bbox(roi, max_px=STEP1_PATCH_PREVIEW_MAX_PX):
        y0, y1, x0, x1 = [int(v) for v in roi]
        h = max(1, y1 - y0)
        w = max(1, x1 - x0)
        if h <= max_px and w <= max_px:
            return y0, y1, x0, x1
        cy = (y0 + y1) // 2
        cx = (x0 + x1) // 2
        hh = min(h, max_px) // 2
        ww = min(w, max_px) // 2
        return cy - hh, cy - hh + min(h, max_px), cx - ww, cx - ww + min(w, max_px)

    def _preload_all_patches(self):
        """Launch a background loader for every patch not yet in cache.
        Multiple threads run concurrently — each opens its own TiffFile handle
        and reads a different tile region, so they do not interfere.
        """
        needed = self._needed_channels()
        if not needed:
            self.prev_status.setText(
                "No visible channels — tick a channel to show it.")
            return

        for idx, roi in enumerate(self._all_patches):
            cache = self._patch_channel_cache.get(idx) or {}
            if idx in self._patch_load_ready and all(ch in cache for ch in needed):
                continue   # already cached
            if idx in self._patch_loaders and self._patch_loaders[idx].isRunning():
                continue   # already loading
            perf_trace.mark("loader.start", who="_preload_all_patches",
                            patch=idx, channels=len(needed),
                            running=sum(1 for t in self._patch_loaders.values()
                                        if t.isRunning()))
            self._start_loader_for(idx, needed=needed)

    def _connect_patch_loader(self, thread, idx):
        """Wire one loader's signals so each answer carries its own identity.

        Every callback is bound to the thread that will emit it.  Disconnecting
        is not enough on its own: a queued signal already sitting in the event
        loop is delivered whatever happens to the connection afterwards, so an
        old worker's late `done` could write the previous dataset's pixels into
        a patch that was cleared, and its late `error` could blame the channels
        the CURRENT worker is reading.  `sender()` is not used for this; the
        identity is captured here, where it is known.
        """
        thread.done.connect(
            lambda patch_idx, cache, t=thread:
                self._on_patch_loaded_from(t, patch_idx, cache))
        thread.progress.connect(
            lambda patch_idx, done, total, ch, t=thread:
                self._on_patch_progress_from(t, patch_idx, done, total, ch))
        thread.error.connect(
            lambda patch_idx, msg, t=thread:
                self._on_patch_error_from(t, patch_idx, msg))
        thread.finished.connect(
            lambda i=idx, t=thread: self._on_patch_loader_finished(i, t))

    def _is_current_patch_loader(self, thread, patch_idx):
        return self._patch_loaders.get(patch_idx) is thread

    def _on_patch_loaded_from(self, thread, patch_idx, cache):
        if not self._is_current_patch_loader(thread, patch_idx):
            print(f"[Step1] dropped a late patch result for P{patch_idx+1}: "
                  "it came from a loader that is no longer current")
            return
        self._on_patch_loaded(patch_idx, cache)

    def _on_patch_progress_from(self, thread, patch_idx, done, total, ch):
        if not self._is_current_patch_loader(thread, patch_idx):
            return
        self._on_patch_progress(patch_idx, done, total, ch)

    def _on_patch_error_from(self, thread, patch_idx, msg):
        if not self._is_current_patch_loader(thread, patch_idx):
            print(f"[Step1] dropped a late patch error for P{patch_idx+1}: "
                  "it came from a loader that is no longer current")
            return
        self._on_patch_error(patch_idx, msg)

    def _start_loader_for(self, idx, needed=None):
        """Start (or restart) the loader thread for patch `idx`."""
        if idx >= len(self._all_patches):
            return
        if needed is None:
            needed = self._needed_channels()
        if not needed:
            return

        # An INCREMENTAL read — this patch is already on screen and is only
        # missing a channel the user has just asked for — must not move the
        # camera. The state is captured HERE, on the GUI thread, against the
        # loader that is about to run, and only that loader may spend it: a
        # late or replaced one would otherwise restore a view the user has
        # since moved. A patch with nothing cached is a FIRST load and still
        # fits, which is what makes P1 -> P2 frame the new rectangle.
        incremental = (idx == self._preview_patch_idx
                       and bool(self._patch_channel_cache.get(idx))
                       and self.prev_img.image is not None)
        pending_view = self._save_patch_preview_view_state() if incremental else None

        # Stop any existing loader for this patch before replacing it.
        if not self._stop_loader_for(idx):
            self.prev_status.setText(f"P{idx+1} is still stopping… please wait")
            return

        patch_bbox = self._all_patches[idx]
        y0, y1, x0, x1 = self._limited_patch_bbox(patch_bbox)
        if self._active_roi and not self._patch_inside_roi_bbox((y0, y1, x0, x1), self._active_roi):
            self.prev_status.setText(f"⚠ P{idx+1} is outside active ROI")
            return
        print(f"[Step1-preview] roi_bbox={(self._active_roi or {}).get('bbox_fullres')}")
        print(f"[Step1-preview] patch_bbox={list(map(int, patch_bbox))}")
        print(f"[Step1-preview] read_region y0,y1,x0,x1={[int(y0), int(y1), int(x0), int(x1)]}")
        print("[Step1] patch preview source: fullres")
        print(f"[Step1] patch bbox: original={list(map(int, patch_bbox))} loaded={[y0, y1, x0, x1]}")
        print(f"[Step1] channels loaded for patch: {needed}")
        # Recorded HERE, not by the caller: `_stop_loader_for` above clears the
        # previous thread's record, so anything written before it would be
        # wiped, leaving a failure with no idea which channels it was reading.
        try:
            t = PreviewLoaderThread(idx, self.loader, needed,
                                    y0, y1, x0, x1,
                                    downsample=1, normalize=False)
            self._connect_patch_loader(t, idx)
            self._patch_loaders[idx] = t
            self._loader_channels[idx] = set(needed)
            if pending_view is not None:
                # Bound to THIS loader: only its own result may spend it.
                self._preserve_view_after_patch_load[idx] = (t, pending_view)
            else:
                self._preserve_view_after_patch_load.pop(idx, None)
            self._set_patch_btn_state(idx, 'loading')
            t.start()
        except Exception:
            # Nothing is in flight, so nothing may claim to be.
            self._patch_loaders.pop(idx, None)
            self._loader_channels.pop(idx, None)
            print(f"[Step1] failed to start the loader for P{idx+1}:\n"
                  f"{traceback.format_exc()}")
            raise
        print("[Step1-preview] full_image_load=False")

    def _on_patch_progress(self, patch_idx, done, total, ch):
        if patch_idx == self._preview_patch_idx:
            self.patch_cache_status.setText(
                f"P{patch_idx+1} loading ({done+1}/{total}): {ch}"
            )

    def _on_patch_loaded(self, patch_idx, cache):
        # Merge, do not replace: a loader may have been sent for one newly
        # ticked channel, and the channels already in hand must survive it.
        held = self._patch_channel_cache.setdefault(patch_idx, {})
        held.update(cache)
        needed = self._needed_channels()
        if all(ch in held for ch in needed):
            self._patch_load_ready.add(patch_idx)
        self._set_patch_btn_state(patch_idx, 'ready')

        nuc_ch, _ = self.config.get_nucleus()
        y0, y1, x0, x1 = self._all_patches[patch_idx]
        h = next(iter(cache.values())).shape[0] if cache else 0
        w = next(iter(cache.values())).shape[1] if cache else 0
        local_txt = ""
        if self._active_roi and self._active_roi.get("bbox_fullres"):
            ry0, _, rx0, _ = [int(v) for v in self._active_roi["bbox_fullres"]]
            local_txt = (
                f" local=[{y0-ry0},{y1-ry0},{x0-rx0},{x1-rx0}]"
            )
        print(f"[Preview] P{patch_idx+1} ready — {len(cache)} ch, {h}×{w} px{local_txt}")
        print(f"[Step1-preview] loaded_shape={(h, w)}")

        # If this is the currently viewed patch, render it immediately
        if patch_idx == self._preview_patch_idx:
            nuc_ok = "✓" if nuc_ch in cache else "✗(not found!)"
            cyto_n = len([c for c in cache if c != nuc_ch])
            self.patch_cache_status.setText(
                f"P{patch_idx+1} ready  nucleus({nuc_ch}){nuc_ok}  "
                f"cyto: {cyto_n} ch  {h}×{w} px (full-res crop)"
            )
            saved = self._preserve_view_after_patch_load.pop(patch_idx, None)
            owner, state = saved if saved else (None, None)
            if state is not None and owner is self._patch_loaders.get(patch_idx):
                # The load this camera was captured for: new pixels, same view.
                self._refresh_patch_preview(reset_view=False)
                self._restore_patch_preview_view_state(state)
            else:
                self._refresh_patch_preview(reset_view=True)

        # Update global status once all patches are loaded
        n_ready = len(self._patch_load_ready)
        n_total = len(self._all_patches)
        if n_ready == n_total:
            self.patch_cache_status.setText(f"All {n_total} patches cached. Click P1-P{n_total} to switch instantly.")

    def _on_patch_error(self, patch_idx, msg):
        # Blame the channels this loader was actually reading, and only those.
        # A tick made while it ran asked for something else and is still owed.
        failed = self._loader_channels.get(patch_idx, set())
        if failed:
            self._failed_channels.setdefault(patch_idx, set()).update(failed)
        self._set_patch_btn_state(patch_idx, 'error')
        if patch_idx == self._preview_patch_idx:
            self.patch_cache_status.setText(f"P{patch_idx+1} load error: {msg}")
        print(f"[Preview] P{patch_idx+1} error: {msg}")

    def _on_patch_loader_finished(self, patch_idx, thread):
        """One loader ended.  Serve a demand that arrived while it ran — once.

        Three things are deliberately NOT retried here: a thread that is no
        longer the one in the slot (it was replaced or stopped, and its late
        exit says nothing about what is wanted now), a patch nobody asked more
        of, and a read that failed.  Any of those looping back into a new load
        is how a failing channel becomes an endless restart, or an old
        dataset's thread starts reading for the new one.
        """
        if self._patch_loaders.get(patch_idx) is not thread:
            return
        self._patch_loaders.pop(patch_idx, None)
        self._loader_channels.pop(patch_idx, None)
        pending = self._pending_channel_demand.pop(patch_idx, set())
        if not pending:
            return
        if self.loader is None or patch_idx != self._preview_patch_idx:
            return
        # Whatever was asked for while this loader ran is still wanted, whether
        # the load succeeded or failed; `_ensure_channels_cached` drops the
        # channels that are now cached or have failed on their own.
        self._ensure_channels_cached(patch_idx)

    def _stop_loader_for(self, idx, timeout_ms=3000):
        self._pending_channel_demand.pop(idx, None)
        self._loader_channels.pop(idx, None)
        thread = self._patch_loaders.get(idx)
        if thread is None:
            return True
        if thread.isRunning():
            thread.stop()
            with perf_trace.span("loader.wait", who="_stop_loader_for",
                                 patch=idx, timeout_ms=int(timeout_ms)):
                thread.wait(timeout_ms)
        if not thread.isRunning() and self._patch_loaders.get(idx) is thread:
            self._patch_loaders.pop(idx, None)
            return True
        return not thread.isRunning()

    def _stop_all_loaders(self):
        self._preload_debounce.stop()
        self._pending_channel_demand.clear()
        self._loader_channels.clear()
        self._failed_channels.clear()
        survivors = {}
        for idx, t in list(self._patch_loaders.items()):
            if t.isRunning():
                t.stop()
                with perf_trace.span("loader.wait", who="_stop_all_loaders",
                                     patch=idx, timeout_ms=3000):
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
        if self._perf_heartbeat is not None:
            self._perf_heartbeat.stop()
            self._perf_heartbeat = None
        # Before the loaders, because it is the cheapest thing here to stop
        # and it holds the dataset's arrays.
        self._stop_compose_worker("the main window is closing")
        # Block01's own, PHASE ONE ONLY: new frame requests are refused so
        # nothing re-arms behind the stop, and nothing is destroyed yet. This
        # close can still be refused a few lines down -- a live overview read
        # cannot be interrupted -- and a window that goes on living must go
        # on having its two shared windows and a coordinator that can draw.
        # The irreversible half is `finalize_close`, at the accept.
        self._display.begin_close("the main window is closing")
        # Step0's own background workers: the geometry writer and the patch
        # readers. Both are request-only, so this neither joins a thread nor
        # can be refused; what it buys is that neither emits into a window
        # that is going away.
        page = self.__dict__.get("_step0")
        stop = getattr(page, "stop_background_jobs", None)
        if stop is not None:
            stop()
        # The sink is NOT shut down here. It closes once, at the end of this
        # method, after every managed loader has physically finished and its
        # own `job.end` is queued -- a shutdown before that is followed by
        # loader lines that start a second writer on a file the first one
        # closed, and a close attempt that ends in `event.ignore()` would
        # stop and restart the writer on every retry.
        self._stop_all_loaders()
        # A fusion job outlives the window unless it is asked to stop and then
        # held: destroying a running QThread is what produces
        # "QThread: Destroyed while thread is still running".
        self._retire_fusion_worker("the main window is closing")
        for worker in list(self._retired_fusion_workers):
            if worker.isRunning():
                worker.wait(3000)
            self._drop_retired_fusion_worker(worker)
        if any(w.isRunning() for w in self._retired_fusion_workers):
            # Closing now would destroy a QThread that is still running. Hold
            # the window open and try again; the job was already asked to stop.
            event.ignore()
            # The window goes on living, so its shared windows go on working.
            self._display.resume()
            self._fusion_lbl.setText("Waiting for the fusion job to stop…")
            QtCore.QTimer.singleShot(500, self.close)
            return
        if self._patch_loaders:
            event.ignore()
            self._display.resume()
            self.prev_status.setText("Waiting for preview loaders to stop…")
            QtCore.QTimer.singleShot(500, self.close)
            return
        # An overview read cannot be interrupted -- it is one blocking call
        # inside the loader -- so the panels keep their workers alive until
        # those physically finish (see `overview_panel`). That keep-alive only
        # moves the problem to interpreter teardown unless the APPLICATION
        # waits for them too: at exit, module teardown releases a running
        # QThread and the process aborts. Same treatment as the fusion job and
        # the patch loaders: hold the window open and try again, rather than
        # block the GUI for seconds. Nothing here waits on a Load, a hidden
        # popup or a gesture -- only on closing the application.
        live_reads = [w for w in overview_panel.live_overview_workers()
                      if w.isRunning()]
        if live_reads:
            event.ignore()
            self._display.resume()
            self.prev_status.setText(
                f"Waiting for {len(live_reads)} tissue overview read(s) to "
                f"finish…")
            QtCore.QTimer.singleShot(500, self.close)
            return
        # The close is CERTAIN from here: nothing above can refuse it any
        # more. Now the irreversible half -- the compose thread is retired and
        # the two shared windows are closed, once.
        self._display.finalize_close("the main window is closing")
        # The sink closes LAST, and finally: every loader has reported its own
        # `job.end` by now, so nothing is left to write and nothing can start
        # a second writer on a file the first one closed. Bounded, because a
        # window must not stay open on a disk.
        perf_trace.shutdown(final=True)
        super().closeEvent(event)

    # ── Force-update (clears all caches, re-loads everything) ───────

    def _force_update_all(self):
        """Update button: wipe all caches and restart preload for all patches."""
        if not self._all_patches:
            self.prev_status.setText("⚠ No patches yet — draw at least one patch first")
            return
        self._stop_all_loaders()
        self._patch_channel_cache.clear()
        self._overlay_display_cache.clear()
        self._signal_cache.clear()
        self._patch_load_ready.clear()
        for i in range(len(self._all_patches)):
            self._set_patch_btn_state(i, 'idle')
        self.patch_cache_status.setText("Cache cleared. Reloading patches...")
        self._preload_all_patches()

    # ── Rendering (pure numpy, no disk IO) ──────────────────────────

    def _save_patch_preview_view_state(self):
        try:
            vr = self.prev_vb.viewRange()
            return {
                "view_range": [[float(vr[0][0]), float(vr[0][1])], [float(vr[1][0]), float(vr[1][1])]],
                "transform": QtGui.QTransform(self.prev_vb.transform()),
            }
        except Exception:
            return None

    def _restore_patch_preview_view_state(self, state):
        if not state:
            return
        try:
            vr = state.get("view_range")
            if vr and len(vr) == 2:
                self.prev_vb.disableAutoRange()
                self.prev_vb.setRange(xRange=vr[0], yRange=vr[1], padding=0)
                print(f"[Step1-preview] restored_view_range={self.prev_vb.viewRange()}")
        except Exception:
            pass

    def save_patch_view_state(self):
        return self._save_patch_preview_view_state()

    def restore_patch_view_state(self, state):
        self._restore_patch_preview_view_state(state)

    # ── preview mode, overlay rendering ────────────────────────────────
    def set_preview_mode(self, mode, force=False, reconcile=True):
        """Switch between the multi-channel overlay and the fusion preview.

        Ticks, weights, colours, the current channel and the camera are left
        alone; no fusion worker is started. The two modes do not need the same
        channels, though, so entering one reads the ones it is missing —
        unless `reconcile=False`, which is for a caller that is about to
        restore more state and wants a single load and a single redraw at the
        end of it.
        """
        mode = STEP1_PREVIEW_FUSION if mode == STEP1_PREVIEW_FUSION else STEP1_PREVIEW_OVERLAY
        changed = (mode != self._step1_preview_mode)
        self._step1_preview_mode = mode
        self._btn_mode_overlay.setChecked(mode == STEP1_PREVIEW_OVERLAY)
        self._btn_mode_fusion.setChecked(mode == STEP1_PREVIEW_FUSION)
        self._preview_title.setText(
            "③ Overlay  (ticked channels, their colours and Intensity window)"
            if mode == STEP1_PREVIEW_OVERLAY else
            "③ Fusion Preview  Red=cyto  Blue=nucleus  (weights apply)")
        if (changed or force) and reconcile:
            # The two modes need different channels.  Arriving in fusion with
            # only the overlay's channels in hand would sit on "Preparing"
            # forever unless the missing ones are asked for here.
            self._ensure_channels_cached(self._preview_patch_idx)
            self._refresh_patch_preview(reset_view=False)
            # The popup follows the mode: Overlay and Fusion are two pictures
            # of the same slide, and the navigator showing the other one is
            # the disagreement this request removes. Nothing is rebuilt --
            # same popup, same camera, same ROI and patch artists.
            self._display.coordinator.request_frame(kind="mode")
            self._schedule_step1_session_save()

    def _drop_overlay_cache_for(self, patch_idx):
        """Forget one patch's remapped images; its raw pixels went too.

        The fusion signals for that patch go with them: they are keyed by
        (patch, channel) so this evicts exactly that patch, and their weak
        references would not keep the dropped arrays alive in any case.
        """
        self._overlay_display_cache.drop_patch(patch_idx)
        self._signal_cache.drop_patch(patch_idx)

    def _refresh_patch_preview(self, reset_view=False):
        """Draw whichever preview the current mode asks for."""
        with perf_trace.span("step1.frame", mode=self._step1_preview_mode,
                             patch=self._preview_patch_idx,
                             reset_view=bool(reset_view)):
            if self._step1_preview_mode == STEP1_PREVIEW_OVERLAY:
                self._render_overlay_patch(reset_view=reset_view)
            else:
                self._render_current_patch(reset_view=reset_view)

    _DISPLAY_KEYS = ("min", "max", "brightness", "contrast", "gamma")

    def _display_window_for(self, channel, remap):
        """The Min/Max/Gamma this channel is shown with, and a key for it.

        The Step0 Intensity window owns these numbers when it has them.  A
        channel it never touched gets a percentile window instead of the raw
        full range, which would otherwise wash the overlay out.

        The key carries the VALUES, not their provenance: a slider moved in the
        Intensity window changes the numbers while the source stays "step0", so
        a key made of labels would keep handing back yesterday's pixels.
        """
        params = (remap or {}).get(channel)
        if params:
            key = tuple((name, _round_display_value(params.get(name)))
                        for name in self._DISPLAY_KEYS)
            return dict(params), key
        return None, ("auto",)

    def _overlay_gray(self, patch_idx, channel, arr, remap):
        """This channel as a [0,1] image, remapped once and kept.

        A thin delegate: the arithmetic and the caching rules live in
        `core.preview_compose`, so the background frame worker runs the same
        code with its own cache instead of a copy of this one.
        """
        return preview_compose.channel_gray(
            patch_idx, channel, arr, remap, self._overlay_display_cache,
            span=perf_trace.span)

    def _effective_fusion_config(self):
        """The one configuration both previews and every Save consume.

        `ConfigPanel.effective_config` drops the unticked channels; the panel
        keeps their weights so re-ticking restores them, but nothing that
        produces pixels or files may see them.
        """
        cfg = self.config
        if hasattr(cfg, "effective_config"):
            return cfg.effective_config()
        return cfg.get_full_config()

    def _overlay_weight(self, channel):
        """How strongly a ticked channel shows: the same 0..1 the fusion uses.

        The nucleus carries its own weight rather than a group's, and a channel
        in several groups shows at the strongest of them, which is what the
        fusion's max-over-groups does with it too.
        """
        nuc_ch, nuc_w = self.config.get_nucleus()
        if channel == nuc_ch:
            return float(nuc_w or 0.0)
        groups = self.config.get_groups()
        group_weights = self.config.get_group_weights()
        best = 0.0
        for gname, ch_weights in groups.items():
            if channel not in ch_weights:
                continue
            gw = float(group_weights.get(gname, 1.0) or 0.0)
            best = max(best, gw * float(ch_weights[channel] or 0.0))
        return min(max(best, 0.0), 1.0)

    def _render_overlay_patch(self, reset_view=False):
        """Additively blend the ticked channels in their own colours, each at
        its own weight.

        One set of numbers for both previews. The overlay used to ignore the
        weight while the fusion ignored the tick, so the same panel described
        two different pictures and neither matched what a Save would write.
        Weight is strength here and contribution there; the channels are the
        ticked ones in both.
        """
        idx = self._preview_patch_idx
        cache = self._patch_channel_cache.get(idx) or {}
        ticked = self.config.visible_channels()
        if not ticked:
            self.prev_img.clear()
            self.prev_status.setText(
                "No visible channels — tick a channel to show it.")
            return
        ready = [ch for ch in ticked if ch in cache]
        if not ready:
            self.prev_img.clear()
            self.prev_status.setText("Loading channels…")
            return

        # Only what this frame draws: `ready` is ticked AND in cache.
        remap = self._display_mapping(channels=ready)
        arrays = {ch: cache[ch] for ch in ready}
        weights = {ch: self._overlay_weight(ch) for ch in ready}
        colors = {ch: _hex_to_rgb01(self.config.channel_color(ch))
                  for ch in ready}
        # The arithmetic lives in `core.preview_compose`, so the background
        # frame worker runs the SAME code rather than a copy of it -- a second
        # copy is how the screen and the saved file drifted apart once
        # already.
        with perf_trace.span("step1.compose", channels=len(arrays)):
            rgb = preview_compose.overlay_rgb_u8(
                idx, arrays, remap, colors, weights,
                self._overlay_display_cache, span=perf_trace.span)
        if rgb is None:
            self.prev_img.clear()
            self.prev_status.setText(
                "No visible channels — tick a channel to show it.")
            return

        first_load = (self.prev_img.image is None)
        preserve = not (first_load or reset_view)
        state = self._save_patch_preview_view_state() if preserve else None
        with perf_trace.span("step1.setImage", kind="overlay"):
            self.prev_img.setImage(rgb, autoLevels=False)
        if first_load or reset_view:
            self.prev_vb.autoRange()
        else:
            self._restore_patch_preview_view_state(state)
        missing = [ch for ch in ticked if ch not in cache]
        self.prev_status.setText(
            f"Overlay: {len(ready)} channel(s)"
            + (f"  ({len(missing)} still loading)" if missing else ""))

    def _on_channel_visibility_changed(self, channel, visible):
        """A tick changed which channels are in play."""
        if self._restoring_display_state:
            return
        if visible:
            # Ticking a channel again is the user asking for it, so a previous
            # failure stops standing in the way.
            for failed in self._failed_channels.values():
                failed.discard(channel)
            self._ensure_channels_cached(self._preview_patch_idx)
        self._refresh_patch_preview(reset_view=False)
        # A tick changes which channels the whole-slide thumbnail composites,
        # and the ones it has never held are asked for in the background by
        # the snapshot rather than read here.
        self._display.coordinator.request_frame(kind="visibility",
                                                channel=channel)
        # A tick changes the effective configuration, so it changes whether the
        # screen and the committed snapshot still agree. Said now, not at the
        # next redraw: a panel claiming "saved" while a search would be refused
        # is the disagreement this label exists to show.
        self._update_fusion_settings_state()
        self._schedule_step1_session_save()

    def _on_current_channel_changed(self, channel):
        """The channel being edited changed: point Intensity at it."""
        if self._restoring_display_state:
            return
        self._ensure_channels_cached(self._preview_patch_idx)
        # Through Block01's display layer. This used to be
        # `self._step0.focus_intensity_on(...)` -- one step reaching into
        # another for a window neither of them owns.
        self._display.focus_intensity(channel)
        self._schedule_step1_session_save()

    def _on_display_mapping_changed(self, channel):
        """One channel's Min/Max/Gamma moved: drop only that channel's pixels.

        Both previews map through these numbers, so both follow them. Only the
        overlay used to redraw, which left the fusion preview showing the
        window the user had just moved away from until something else forced a
        redraw. Coalesced like every other state change: a slider drag leaves
        one composite of where the user stopped.
        """
        if not channel:
            return
        rev = perf_trace.REVISIONS.bump("display_mapping")
        perf_trace.mark("step1.mapping_in", channel=channel, rev=rev)
        # Only this channel's arrays: every other channel is the same pixels
        # through the same numbers.
        self._overlay_display_cache.drop_channel(channel)
        self._signal_cache.drop_channel(channel)
        if channel in self.config.visible_channels():
            self._schedule_preview_update()
        # The thumbnail maps through the same numbers and follows them
        # through the shared state, which requests its frame -- so this
        # window does not have to remember to. The compose worker keys its
        # cache by the window's VALUES, so a moved window misses for this
        # channel and every other channel hits; the raw whole-slide arrays
        # are not re-read.

    def _on_display_mapping_committed(self, payload):
        """The draft is now on disk and named by a fresh manifest hash.

        Nothing to redraw — the pixels were already being drawn with these
        numbers — but the committed file has changed, so anything derived from
        the old one is dropped.
        """
        payload = dict(payload or {})
        self._remap_cache = None
        print(f"[Step1] display mapping committed for "
              f"{len(payload.get('channels') or [])} channel(s)")

    def _on_channel_color_changed(self, channel, color):
        """One colour, wherever it is changed.

        The Intensity window draws its histogram in the channel's colour and
        used to take Step0's palette while the overlay used this panel's, so
        the same channel came up in two colours. Step1 owns the overlay colour,
        so it is pushed; and a colour changed in the Intensity window comes
        back the same way.
        """
        if self._restoring_display_state:
            return
        # The panel has already written Block01's shared state, which is what
        # every view reads; `_on_shared_channel_color_changed` is where the
        # pictures follow. Nothing is pushed at Step0 from here -- two steps
        # emitting at each other is how one channel came to have two colours.
        self._display.state.set_color(channel, color, origin="step1")
        self._schedule_step1_session_save()

    def _on_step0_channel_color_changed(self, channel, color):
        """A colour picked in the Intensity window (or Step0) comes back.

        Kept as the adapter for Step0's own signal, and it does one thing:
        settle the value in the shared state. Whether that CHANGES anything
        is the state's decision, so an echo of a colour Step1 just chose ends
        here instead of starting a second lap.
        """
        if self._restoring_display_state or not channel:
            return
        self._display.state.set_color(channel, color, origin="step0")

    def _ensure_channels_cached(self, idx):
        """Read what this patch is missing — only that, and only once.

        A channel whose read already failed for this patch is left alone: the
        retry would fail the same way.  A channel wanted while another load is
        in flight is remembered BY NAME, so the outcome of that load, good or
        bad, cannot take the new request with it.
        """
        if self.loader is None or idx < 0 or idx >= len(self._all_patches):
            return
        needed = self._needed_channels()
        if not needed:
            return
        cache = self._patch_channel_cache.get(idx) or {}
        failed = self._failed_channels.get(idx, set())
        missing = [ch for ch in needed if ch not in cache and ch not in failed]
        if not missing:
            if all(ch in cache for ch in needed):
                self._patch_load_ready.add(idx)
            return
        loader = self._patch_loaders.get(idx)
        if loader is not None and loader.isRunning():
            # One loader per patch.  Whatever this one is not fetching is
            # remembered until it leaves the slot.
            inflight = self._loader_channels.get(idx, set())
            wanted = {ch for ch in missing if ch not in inflight}
            if wanted:
                self._pending_channel_demand.setdefault(idx, set()).update(wanted)
            return
        self._pending_channel_demand.pop(idx, None)
        perf_trace.mark("loader.start", who="_ensure_channels_cached",
                        patch=idx, channels=len(missing),
                        running=sum(1 for t in self._patch_loaders.values()
                                    if t.isRunning()))
        self._start_loader_for(idx, needed=missing)

    def _show_intensity_window(self):
        """Open the shared Intensity window on the current channel.

        Through Block01's display layer, and only through it. There is one
        Intensity window for the process and one set of Min/Max/Gamma behind
        it, editable from every step; `show_intensity` points it at the
        channel, so nothing here reaches into Step0 to do it a second time.
        """
        return self._display.show_intensity(self.config.current_channel())

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
        missing = [ch for ch in self._needed_channels() if ch not in cache]
        if missing:
            # Drawing now would publish a nucleus-only picture that looks like
            # the finished fusion.  Keep whatever is on screen and say so.
            self.prev_status.setText(
                f"Preparing fusion — {len(missing)} channel(s) still loading…")
            return
        # The EFFECTIVE configuration: an unticked channel is out of the
        # picture, so it is out of the fusion too. The preview used to read
        # weights alone, so a channel the user had hidden still went into the
        # fused result and into fused.zarr.
        effective = self._effective_fusion_config()
        groups = {name: dict(data.get("channels") or {})
                  for name, data in (effective.get("groups") or {}).items()}
        group_weights = {name: float(data.get("group_weight", 1.0) or 0.0)
                         for name, data in (effective.get("groups") or {}).items()}
        nuc = effective.get("nucleus") or {}
        nuc_ch, nuc_w = nuc.get("channel", ""), float(nuc.get("weight", 0.0) or 0.0)
        shape = next(iter(cache.values())).shape

        # Reflect the manual Channel Remap (Step0) in the on-screen preview too,
        # mirroring the disk FullFusionWorker: conditioned channels use
        # apply_channel_remap (Min/Max/Gamma); others use the percentile norm.
        # Map each channel once, then hand the signals to THE fusion core.  The
        # arithmetic below used to be written out here as well; a second copy of
        # it is how the screen and the saved file drifted apart.
        wanted = {nuc_ch} if nuc_ch else set()
        for ch_weights in groups.values():
            wanted.update(ch_weights.keys())
        # Only the channels this frame fuses, and only those already in cache:
        # asking for the rest makes the render callback seed an automatic
        # window per channel from whole-slide pixels (7.47 s of one measured
        # frame).
        remap = self._display_mapping(
            channels=[ch for ch in wanted if ch in cache])
        with perf_trace.span("step1.signals", channels=len(wanted)):
            signals = {ch: self._preview_channel_signal(ch, cache[ch], remap)
                       for ch in wanted if ch in cache}
        with perf_trace.span("step1.fuse", channels=len(signals)):
            cyto, nuc = fuse_channels(signals, groups, group_weights,
                                      nuc_ch, nuc_w)
        if cyto is None:
            cyto = np.zeros(shape, dtype=np.float32)
            nuc = np.zeros(shape, dtype=np.float32)

        if float(cyto.max()) <= 0.0 and float(nuc.max()) <= 0.0:
            # Say so; do not draw something else. Substituting the raw nucleus
            # here was a second set of fusion semantics that the disk worker
            # does not have, so an all-zero configuration looked different on
            # screen from the file it would save.
            self.prev_status.setText(
                "This configuration fuses to nothing: every weight is 0.")

        if cyto is None and nuc is None:
            return
        rgb = self.fusion.to_rgb(cyto, nuc)
        rgb_u8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8) if rgb.dtype.kind == "f" else np.asarray(rgb, dtype=np.uint8)
        print("[Step1-FusionPreview] update image only, masks ignored")
        print("[Step1-FusionPreview] no mask overlay rendered")

        # Only reset view on first load or explicit patch switch;
        # weight/group changes preserve the current zoom & pan.
        first_load = (self.prev_img.image is None)
        preserve_view = not (first_load or reset_view)
        state = None if not preserve_view else self._save_patch_preview_view_state()
        print(f"[Step1-preview] preserve_view={preserve_view}")
        print(f"[Step1-preview] old_view_range={(state or {}).get('view_range')}")
        print(f"[Step1-preview] reset_view={bool(first_load or reset_view)}")
        with perf_trace.span("step1.setImage", kind="fusion"):
            self.prev_img.setImage(rgb_u8, autoLevels=False)
        if first_load or reset_view:
            self.prev_vb.autoRange()
        else:
            self._restore_patch_preview_view_state(state)

    def _on_cfg_changed(self):
        """A weight or the nucleus channel changed.

        BOTH previews read weights now, so both redraw. A weight that has just
        risen above zero can make one channel newly necessary, and that one
        channel is read; everything already in hand stays, and nothing is
        evicted.
        """
        self._ensure_channels_cached(self._preview_patch_idx)
        self._schedule_preview_update()
        # The Tissue Preview shows the same weights over the whole slide, so
        # it follows the same drag. Its own frame clock: the two pictures are
        # not required to publish in the same millisecond, only to end on the
        # same revision.
        self._display.coordinator.request_frame(kind="weight")
        # The shared editor is a view over these numbers, so it follows them
        # wherever they were changed -- including from the channel panel.
        self._refresh_weight_editor()
        self._schedule_step1_session_save()

    def _reset_frame_clock(self):
        """Forget everything the frame clock was doing.

        Stopping the timer is not enough: a pending revision, a merge count
        or an `in_flight` left standing from the old dataset would make the
        new one's first input believe a frame is already due (or already
        running, which silently drops it). The time base goes too, so the
        first input after a dataset switch is a leading edge rather than a
        slot measured from the previous slide's last frame.
        """
        try:
            self._prev_timer.stop()
        except RuntimeError:
            pass
        self._preview_update_pending = False
        self._frame_pending_rev = None
        self._frame_drawing_rev = None
        self._frame_coalesced = 0
        self._frame_in_flight = False
        self._frame_inflight_request = None
        self._frame_last_publish = 0.0
        self._frame_input_at = 0.0

    def _frame_now(self):
        """The clock the frame scheduler runs on. A seam, so a test can drive
        two seconds of input without waiting two seconds."""
        return time.monotonic()

    def _arm_frame_timer(self, wait_ms):
        """Ask for the next frame slot. A seam, for the same reason.

        Rounded UP: `int()` on a 32.7 ms wait asks for 32 and the slot fires
        before the budget has elapsed, which over a long drag drifts the
        clock faster than the frame cost it was chosen against.
        """
        self._prev_timer.start(int(math.ceil(max(0.0, float(wait_ms)))))

    def _schedule_preview_update(self, delay_ms=None):
        """Record that the picture is out of date, and publish on the clock.

        Three rules, and the measured failure each one answers:

        * LEADING EDGE. The first input after an idle period draws
          immediately. The old code always waited 60 ms, so a single slider
          step -- or the first step of a drag -- showed nothing for 60 ms.
        * A FIXED SLOT, NOT A RESTARTED WAIT. While input keeps coming the
          armed timer is left alone. It used to be restarted by every event,
          which is why a continuous drag published only when the hand paused
          (2.2 FPS in overlay, 2.9 in fusion, measured over 653 inputs).
        * LATEST ONLY, DEPTH ONE. What is pending is a revision number, not
          a queue: those 653 inputs would otherwise be 653 frames of history
          nobody will see. How many were merged into the next frame is
          recorded, so "how many were skipped" is a number.

        `delay_ms` is for callers that mean a specific wait -- a dataset
        switch, a session restore -- and it is a FLOOR, never a reset: a
        caller asking for 60 ms cannot push back a slot already due.
        """
        self._preview_update_pending = True
        self._frame_input_rev += 1
        if self._frame_pending_rev is not None:
            self._frame_coalesced += 1
        self._frame_pending_rev = self._frame_input_rev
        self._frame_input_at = self._frame_now()

        since = (self._frame_input_at - self._frame_last_publish) * 1000.0
        wait = max(max(0.0, PREVIEW_FRAME_MS - since), float(delay_ms or 0.0))
        try:
            armed = bool(self._prev_timer.isActive())
        except RuntimeError:
            armed = False
        perf_trace.mark("step1.schedule", rev=self._frame_pending_rev,
                        wait_ms=wait, armed=armed,
                        in_flight=self._frame_in_flight,
                        coalesced=self._frame_coalesced)
        # Said immediately, not after the redraw: the moment the settings differ
        # from the snapshot, a search must not look startable.
        self._update_fusion_settings_state()
        if self._frame_in_flight or armed:
            return          # a frame is already due; this input rides it
        if wait <= 0.0:
            self._apply_pending_preview_update()
            return
        self._arm_frame_timer(wait)

    def _stop_compose_worker(self, reason=""):
        """Retire the frame thread, without reaching into it.

        Necessary because its cache holds references to the dataset's raw
        arrays, so a dataset switch has to let go of them -- but the CLEARING
        is the worker's own job, on its own thread, as it exits. Doing it from
        here would be the GUI thread emptying a cache a frame may be reading,
        which is the very thing keeping the cache private was for.

        A worker that does not stop in time is kept referenced rather than
        forced: it is a daemon thread finishing one array, it emits nothing
        once asked to stop, and dropping the last reference to its QObject
        while the thread runs is how a live thread loses the object its
        signals belong to.
        """
        worker, self._compose_worker = self._compose_worker, None
        self._frame_inflight_request = None
        self._frame_in_flight = False
        if worker is None:
            return
        if reason:
            print(f"[Step1] frame worker retired: {reason}")
        try:
            worker.done.disconnect()
            worker.failed.disconnect()
        except Exception:                                   # noqa: BLE001
            pass
        stopped = False
        try:
            stopped = bool(worker.stop())
        except Exception:                                   # noqa: BLE001
            pass
        if not stopped:
            # Still composing. Hold it so its QObject outlives its thread,
            # and drop it the next time we look.
            self._retired_compose_workers = [
                held for held in getattr(self, "_retired_compose_workers", [])
                if held.isRunning()]
            self._retired_compose_workers.append(worker)
            print("[Step1] frame worker still composing; holding a reference "
                  "until it finishes")

    def _compose_worker_ready(self):
        """The frame thread, started on first use, or None if it cannot run.

        Lazy because a window that never shows a preview should not carry a
        thread, and because a test driving the synchronous path must not have
        one appear behind it.
        """
        worker = self._compose_worker
        if worker is not None:
            return worker
        try:
            # No parent: a QObject destroyed with the window while its
            # thread is still composing would leave that thread emitting
            # through a deleted C++ object.
            worker = PreviewComposeWorker()
            worker.done.connect(self._on_preview_frame)
            worker.failed.connect(self._on_preview_frame_failed)
            worker.start()
        except Exception as exc:                            # noqa: BLE001
            print(f"[Step1] frame worker unavailable ({exc}); "
                  "frames will be composed on the GUI thread")
            return None
        self._compose_worker = worker
        return worker

    def _frame_snapshot(self, rev):
        """Everything one frame needs, as values the GUI thread is done with.

        Returns None when the frame is not a matter of arithmetic -- nothing
        ticked, nothing cached, a patch still loading -- because those cases
        are about what the STATUS BAR should say, and that belongs with the
        rest of the widget work.

        The arrays go in by reference and are never written to: a new read
        replaces the array object in `_patch_channel_cache` rather than
        filling the old one, which is what makes handing them to a thread
        safe and what `array_ids` lets the result be checked against.
        """
        idx = self._preview_patch_idx
        cache = self._patch_channel_cache.get(idx) or {}
        if not cache:
            return None
        mode = self._step1_preview_mode
        if mode == STEP1_PREVIEW_OVERLAY:
            ticked = self.config.visible_channels()
            ready = [ch for ch in ticked if ch in cache]
            if not ready or len(ready) != len(ticked):
                return None          # a status line, not a frame
            weights = {ch: self._overlay_weight(ch) for ch in ready}
            if not any(w > 0 for w in weights.values()):
                return None
            arrays = {ch: cache[ch] for ch in ready}
            payload = {
                "colors": {ch: _hex_to_rgb01(self.config.channel_color(ch))
                           for ch in ready},
                "weights": weights,
                "groups": {}, "group_weights": {},
                "nucleus": ("", 0.0),
            }
        else:
            if idx not in self._patch_load_ready:
                return None
            if [ch for ch in self._needed_channels() if ch not in cache]:
                return None
            effective = self._effective_fusion_config()
            groups = {name: dict(data.get("channels") or {})
                      for name, data in (effective.get("groups") or {}).items()}
            group_weights = {
                name: float(data.get("group_weight", 1.0) or 0.0)
                for name, data in (effective.get("groups") or {}).items()}
            nuc = effective.get("nucleus") or {}
            nuc_ch = nuc.get("channel", "")
            wanted = {nuc_ch} if nuc_ch else set()
            for ch_weights in groups.values():
                wanted.update(ch_weights.keys())
            arrays = {ch: cache[ch] for ch in wanted if ch in cache}
            if not arrays:
                return None
            payload = {
                "colors": {}, "weights": {},
                "groups": groups, "group_weights": group_weights,
                "nucleus": (nuc_ch, float(nuc.get("weight", 0.0) or 0.0)),
            }
        # ALREADY-COMPUTED WINDOWS ONLY, and nothing else this thread could
        # be made to do. `_display_mapping` with its default completes the
        # draft by working out an automatic window for any channel it has not
        # seen -- a whole-slide read AND a percentile pass, measured at
        # ~267 ms per channel, inside the callback whose whole job is to hand
        # a snapshot over and return. `resident_only` only stops the READ; if
        # the array happens to be resident the percentile still runs here,
        # which is most of that cost. So: the draft, plus the windows already
        # memoised, and a channel without one is named `provisional` and
        # drawn by the worker from the patch it is holding. When the real
        # window is computed, the mapping change schedules a new frame.
        remap = self._display_mapping(channels=list(arrays),
                                      computed_only=True) or {}
        self._frame_request_id += 1
        snapshot = {
            "request_id": self._frame_request_id,
            "rev": rev,
            "mode": mode,
            "patch": idx,
            "dataset_gen": self._dataset_gen,
            "arrays": arrays,
            "array_ids": tuple(sorted((ch, id(arr))
                                      for ch, arr in arrays.items())),
            "channels": tuple(sorted(arrays)),
            "remap": copy.deepcopy(remap),
            # Channels drawn from the patch because their whole-slide window
            # is not ready: named, so a reader of the timeline can tell a
            # provisional frame from a final one.
            "provisional": tuple(sorted(ch for ch in arrays
                                        if ch not in remap)),
            "fallback_norm": (self.loader._norm if self.loader is not None
                              else (lambda a: a)),
            "to_rgb": self.fusion.to_rgb,
        }
        snapshot.update(payload)
        return snapshot

    def _dispatch_frame(self, snapshot):
        """Hand a snapshot to the frame thread. A seam, so a test can deliver
        results under its own control."""
        worker = self._compose_worker_ready()
        if worker is None:
            return False
        return bool(worker.submit(snapshot))

    def _apply_pending_preview_update(self):
        """Publish the newest state, once, and leave the next slot to the
        next input.

        The frame itself is composed on `PreviewComposeWorker`: this builds a
        snapshot, hands it over and returns, so a 42-85 ms frame -- measured
        -- is no longer 42-85 ms of GUI thread. `_frame_in_flight` stays true
        until the result arrives, which is what keeps it single-flight; input
        that arrives meanwhile replaces the pending revision and rides the
        slot after the result.

        Frames the worker cannot answer -- nothing ticked, a patch still
        loading, no worker at all -- are drawn here, synchronously, because
        what those cases need is the status bar rather than pixels.

        Re-entrancy is the thing to get right: drawing runs Qt code, Qt can
        deliver a queued timeout inside it, and that event must NOT start a
        second frame.
        """
        if self._frame_in_flight:
            return
        rev = self._frame_pending_rev
        if rev is None:
            self._preview_update_pending = False
            return
        coalesced, self._frame_coalesced = self._frame_coalesced, 0
        self._frame_pending_rev = None
        self._frame_drawing_rev = rev
        self._preview_update_pending = False
        self._frame_in_flight = True

        snapshot = None
        try:
            snapshot = self._frame_snapshot(rev)
        except Exception as exc:                            # noqa: BLE001
            print(f"[Step1] could not snapshot the frame: {exc}")
        if snapshot is not None:
            snapshot["coalesced"] = coalesced
            snapshot["input_rev"] = self._frame_input_rev
            snapshot["input_at"] = self._frame_input_at
            snapshot["dispatched_at"] = self._frame_now()
            self._frame_inflight_request = snapshot["request_id"]
            perf_trace.mark("step1.dispatch", rev=rev,
                            request=snapshot["request_id"],
                            mode=snapshot["mode"], patch=snapshot["patch"],
                            channels=len(snapshot["arrays"]),
                            provisional=len(snapshot["provisional"]),
                            coalesced=coalesced)
            if self._dispatch_frame(snapshot):
                return          # published when the pixels come back
            self._frame_inflight_request = None

        # Degraded, and deliberately: on this path the status text is the
        # answer, so the GUI thread draws it.
        try:
            with perf_trace.span("step1.publish", rev=rev,
                                 mode=self._step1_preview_mode,
                                 coalesced=coalesced, where="gui",
                                 input_rev=self._frame_input_rev,
                                 latency_ms=(self._frame_now()
                                             - self._frame_input_at) * 1000.0):
                self._refresh_patch_preview(reset_view=False)
        finally:
            self._frame_in_flight = False
            self._frame_last_publish = self._frame_now()
        self._update_fusion_settings_state()
        if self._frame_pending_rev is not None:
            self._arm_frame_timer(PREVIEW_FRAME_MS)

    def _frame_result_is_current(self, result):
        """Is this result about the pixels the window would draw NOW?

        STRUCTURAL identity only, and that distinction is the whole design:

        * dataset generation, patch, mode, the channel set and the identity of
          every source array -- if any of these moved, the result describes
          something that is no longer on screen and must be dropped.
        * the Intensity revision is NOT part of it. During a drag a newer
          input has almost always arrived by the time a frame finishes, so
          dropping results for being a revision behind would starve the screen
          exactly while the hand is moving -- the complaint this work exists
          to fix. A frame one revision old is a good intermediate frame; the
          newest revision is already pending and gets the next slot.
        """
        if result.get("dataset_gen") != self._dataset_gen:
            return False
        if result.get("mode") != self._step1_preview_mode:
            return False
        if result.get("patch") != self._preview_patch_idx:
            return False
        cache = self._patch_channel_cache.get(self._preview_patch_idx) or {}
        if result.get("mode") == STEP1_PREVIEW_OVERLAY:
            if tuple(sorted(self.config.visible_channels())) != tuple(
                    result.get("channels") or ()):
                return False
        for channel, ident in result.get("array_ids") or ():
            arr = cache.get(channel)
            if arr is None or id(arr) != ident:
                return False
        return True

    def _on_preview_frame(self, result):
        """Pixels from the frame thread: check, publish, and space the next.

        The camera is read and written HERE and nowhere else. A view range
        belongs to the widget, and asking a worker for one would be asking
        another thread what the user is looking at.
        """
        result = dict(result or {})
        if result.get("request_id") == self._frame_inflight_request:
            self._frame_in_flight = False
            self._frame_inflight_request = None
        rgb = result.get("rgb")
        current = self._frame_result_is_current(result)
        perf_trace.mark("step1.frame_in", rev=result.get("rev"),
                        request=result.get("request_id"),
                        current=current, drawn=rgb is not None,
                        pending=self._frame_pending_rev)
        if not current:
            # Structurally stale: the dataset, patch, mode, channel set or the
            # source arrays have moved on.
            self._maybe_arm_next_frame()
            return
        if rgb is None:
            self._refresh_patch_preview(reset_view=False)
            self._frame_last_publish = self._frame_now()
            self._maybe_arm_next_frame()
            return
        with perf_trace.span(
                "step1.publish", rev=result.get("rev"),
                mode=result.get("mode"), where="worker",
                coalesced=result.get("coalesced"),
                input_rev=result.get("input_rev"),
                latency_ms=(self._frame_now()
                            - float(result.get("input_at")
                                    or self._frame_input_at)) * 1000.0):
            first_load = (self.prev_img.image is None)
            state = (None if first_load
                     else self._save_patch_preview_view_state())
            with perf_trace.span("step1.setImage", kind=result.get("mode")):
                self.prev_img.setImage(rgb, autoLevels=False)
            if first_load:
                self.prev_vb.autoRange()
            else:
                self._restore_patch_preview_view_state(state)
            self._frame_status(result)
        self._frame_last_publish = self._frame_now()
        self._update_fusion_settings_state()
        self._maybe_arm_next_frame()

    def _frame_status(self, result):
        """What the status bar says about a published frame."""
        if result.get("mode") == STEP1_PREVIEW_OVERLAY:
            cache = self._patch_channel_cache.get(
                self._preview_patch_idx) or {}
            ticked = self.config.visible_channels()
            missing = [ch for ch in ticked if ch not in cache]
            self.prev_status.setText(
                f"Overlay: {len(result.get('channels') or ())} channel(s)"
                + (f"  ({len(missing)} still loading)" if missing else ""))
        elif result.get("fused_to_nothing"):
            self.prev_status.setText(
                "This configuration fuses to nothing: every weight is 0.")

    def _on_preview_frame_failed(self, payload):
        payload = dict(payload or {})
        request = dict(payload.get("request") or {})
        if request.get("request_id") == self._frame_inflight_request:
            self._frame_in_flight = False
            self._frame_inflight_request = None
        print(f"[Step1] frame compose failed: {payload.get('error')}")
        perf_trace.mark("step1.frame_failed", rev=request.get("rev"),
                        request=request.get("request_id"))
        self._maybe_arm_next_frame()

    def _maybe_arm_next_frame(self):
        """A revision waiting for a slot gets one, spaced by the clock."""
        if self._frame_pending_rev is None:
            return
        since = (self._frame_now() - self._frame_last_publish) * 1000.0
        self._arm_frame_timer(max(0.0, PREVIEW_FRAME_MS - since))

    # ── Phase 1 ─────────────────────────────────────────────────────

    def _run_p1(self, diameters):
        """
        diameters is a single-element list: [None] for auto, [float] for override.
        Phase 1 now runs ONE inference per patch (not a grid search),
        showing the auto-diameter result for the user to visually confirm.
        After completion, diameter is automatically passed to Phase 2.
        """
        if not self._require_committed_fusion_settings("Phase 1"):
            return
        self._show_step1_patch_results_tab("phase1")
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
        if not self._require_committed_fusion_settings("Phase 2"):
            return
        self._show_step1_patch_results_tab("phase2")
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
        if not self._require_committed_fusion_settings("A patch preview"):
            return
        self._show_step1_patch_results_tab("run_patch_preview")
        method = (params or {}).get("method", CELLPOSE_WHOLECELL_FUSION)
        if method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION):
            QMessageBox.information(
                self, "Segmentation mode",
                "Use Phase 1 / Phase 2 for Cellpose patch preview."
            )
            return
        if method in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2, CELLPOSE_NUCLEI_CSD):
            params = normalize_segmentation_config(params or {})
            hq_channels = parse_hq_channels(params.get("hq_channels") or [])
            if not hq_channels:
                QMessageBox.warning(
                    self,
                    "HQ channels required",
                    "Please enter HQ channels, e.g. PanCK;CD45;CD68",
                )
                return
            try:
                validate_hq_channels(hq_channels, self.loader.channel_names() if self.loader else [])
            except Exception as e:
                QMessageBox.warning(self, "Missing channels", str(e))
                return
        patches = list(self._all_patches)
        if not patches:
            QMessageBox.warning(self, "Info", "Please add at least one Patch first")
            return
        params = normalize_segmentation_config(params or {})
        # Stamped from the snapshot this run is about to be launched with; the
        # gate above has already refused a dirty draft, so these are the same
        # settings `_launch_worker` will stamp into the tasks.
        self._p2_params = self._stamp_with_fusion_settings(params)
        self._params_source = "direct_patch_preview"
        self._check_save_unlock()

        nuc_ch, _ = self.config.get_nucleus()
        print(f"[Step1] patch preview mode={method}")
        print(f"[Step1] preview patches={len(patches)}")
        print(f"[Step1] input=DAPI channel={nuc_ch}")
        print(f"[Step1] params={params.get('params') or params}")

        tasks = [
            (pi, roi, dict(params))
            for pi, roi in enumerate(patches)
        ]
        self.result_grid.setup_grid(
            len(patches),
            [dict(params)],
            f"Patch Preview — {method}  ({len(patches)} patch(es))"
        )
        self._launch_worker(tasks)

    # ── Worker ──────────────────────────────────────────────────────

    def _launch_worker(self, tasks):
        if self.proc is not None and self.proc.is_alive():
            return
        # THE SNAPSHOT, copied here on the GUI thread and never read again:
        # the panel and the Intensity window stay live for the user, and this
        # job keeps the settings it was started with for as long as it runs.
        snapshot = self._committed_fusion_settings()
        if not snapshot:
            print("[Step1] refusing to launch: no committed fusion settings")
            return
        fcfg = copy.deepcopy(snapshot.get("fusion_config") or {})
        groups = {name: dict(data.get("channels") or {})
                  for name, data in (fcfg.get("groups") or {}).items()}
        group_weights = {name: float(data.get("group_weight", 1.0) or 0.0)
                         for name, data in (fcfg.get("groups") or {}).items()}
        nucleus = fcfg.get("nucleus") or {}
        nuc_ch = nucleus.get("channel", "")
        nuc_w = float(nucleus.get("weight", 0.0) or 0.0)
        # Stamped into the TASKS, not onto the result when somebody clicks it.
        # The worker returns these params verbatim, so the history, the result
        # grid and whatever the user selects all carry the hash of the settings
        # that actually produced them — rather than the hash of whatever ran
        # most recently, which is a guess.
        stamp = snapshot.get("hash")
        tasks = [(pi, roi, dict(params, fusion_settings_hash=stamp))
                 for pi, roi, params in tasks]
        self._running_fusion_settings_hash = stamp
        self._proc_stopped = False
        self._proc_queue = mp.Queue()
        self._proc_stop_flag = mp.Event()
        args = {
            "tasks": tasks,
            "ome_path": self.loader.filepath,
            "name_map": self.loader.name_map,
            "correction_config": self.loader.correction_config,
            "groups": groups,
            "group_weights": group_weights,
            "nuc_ch": nuc_ch,
            "nuc_w": nuc_w,
            "corrected_zarr_path": self._corrected_zarr_path,
            "corrected_decisions": self._corrected_decisions,
            "channel_remap_params": copy.deepcopy(
                snapshot.get("display_mapping") or {}),
            "output_dir": (self.step0_output or {}).get("output_dir") or OUTPUT_DIR,
        }
        first_method = ""
        if tasks:
            try:
                first_method = normalize_segmentation_config(tasks[0][2]).get("method", "")
            except Exception:
                first_method = ""
        target = run_mesmer_patch_preview if first_method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED) else run_cellpose_process
        self.proc = mp.Process(
            target=target,
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
            phase_dbg = item.get("phase")
            if phase_dbg is None and isinstance(item.get("params"), dict):
                phase_dbg = item["params"].get("_phase")
            print(f"[Phase2-debug] queue message type={kind}")
            print(f"[Phase2-debug] phase={phase_dbg}")
            print(f"[Phase2-debug] patch_idx={item.get('patch_idx')}")
            print(f"[Phase2-debug] has masks={item.get('masks') is not None}")
            print(f"[Phase2-debug] has rgb_raw={item.get('rgb_raw') is not None}")
            print(f"[Phase2-debug] result_path={item.get('result_path', '')}")
            if kind == "result":
                print(f"[Step1] received result patch_idx={item.get('patch_idx')}")
                masks = item.get("masks")
                self._update_patch_phase_result(item)
                active_updated = self._record_segmentation_preview_result(
                    item.get("patch_idx", 0),
                    item.get("params") or {},
                    masks,
                )
                if active_updated:
                    self._params_source = "patch_preview"
                    self._check_save_unlock()
                self._schedule_step1_session_save()
                if masks is not None:
                    params = item.get("params") or {}
                    method = params.get("method")
                    if method and method != CELLPOSE_WHOLECELL_FUSION:
                        print(f"[Step1] cells={int(np.asarray(masks).max())}")
                self.result_grid.add_result(
                    item["patch_idx"],
                    item["params"],
                    item.get("rgb_overlay"),
                    self._cellpose_result_rgb_for_grid(item),
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
        phase_required = method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION)
        is_stardist = method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION)
        workflow = {
            CELLPOSE_WHOLECELL_FUSION: "wholecell_phase1_phase2",
            CELLPOSE_NUCLEI_DAPI: "nuclei_cellpose",
            CELLPOSE_NUCLEI_EXPANSION: "nuclei_cellpose_expansion",
            CELLPOSE_NUCLEI_HQ: "cellpose_nuclei_hq_patch_preview",
            CELLPOSE_NUCLEI_HQ2: "cellpose_nuclei_hq2_patch_preview",
            CELLPOSE_NUCLEI_CSD: "cellpose_nuclei_csd_patch_preview",
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
        QTimer.singleShot(0, lambda: self._log_step1_layout("after method change sizes"))

    def _on_param_sel(self, params):
        if params.get("_phase") == 1:
            # Auto-unlock Phase 2 with the diameter used in Phase 1
            # (may be None for auto-diameter mode)
            diam = params.get("diameter")   # None or float
            self._p1_diam = diam
            self.search.set_p2_diam(diam)
        else:
            # Phase 2 grid selection
            # Whatever the result carries, unchanged: the hash belongs to the
            # run that produced it and must not be re-guessed here.
            self._p2_params    = params
            self._params_source = "phase2"
            self._check_save_unlock()
        self._schedule_step1_session_save()

    def _stamp_with_fusion_settings(self, params):
        """Record WHICH fusion settings produced these segmentation params.

        A search runs on the settings that were committed when it started. If
        the user then changes and re-saves them, those params describe a
        picture that no longer exists, and pairing them with the new fusion is
        exactly the mix-up the commit point was added to prevent.
        """
        params = dict(params or {})
        if params.get("fusion_settings_hash"):
            return params              # it already knows; never overwrite
        snapshot = self._committed_fusion_settings() or {}
        stamp = snapshot.get("hash")
        if stamp:
            params["fusion_settings_hash"] = stamp
        return params

    #: Where segmentation params can come from without having been searched.
    HANDWRITTEN_PARAM_SOURCES = ("manual", "loaded", "direct_method")

    def _params_match_committed_settings(self):
        """Were the chosen segmentation params produced on these settings?

        Params typed in or loaded from a file were not produced by a search and
        make no claim about any fusion. Params from a search must carry the
        hash of the settings it ran on: no hash means the record is from before
        this was stamped, or from a path that lost it, and neither can be shown
        to match.
        """
        params = self._p2_params or {}
        stamp = str(params.get("fusion_settings_hash") or "")
        source = str(self._params_source or "")
        if not stamp:
            return source in self.HANDWRITTEN_PARAM_SOURCES
        snapshot = self._committed_fusion_settings() or {}
        return stamp == snapshot.get("hash")

    # ── the fusion settings: a draft on screen, a snapshot for the workers ──

    def _handoff_identity(self):
        """WHAT the handoff says right now, not merely where it lives.

        The manifest keeps one stable path and Step0 republishes it in place, so
        a path and a source identity can be identical across a republish that
        changed the ROI, the geometry or the display mapping. The manifest's own
        contents are what actually changed, so they are what a snapshot is bound
        to. Returns None when there is nothing to be bound to.
        """
        s0 = self.step0_output or {}
        path = str(s0.get("step0_manifest_path") or "")
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                manifest = json.load(f) or {}
        except Exception as exc:                            # noqa: BLE001
            print(f"[Step1] could not read the manifest to bind against: {exc}")
            return None
        # The whole manifest, minus what changes without meaning anything.
        return {
            "manifest_path": os.path.abspath(path),
            "manifest_digest": self._step1_config_hash(manifest),
            "channel_remap_config_hash": str(
                manifest.get("channel_remap_config_hash")
                or s0.get("channel_remap_config_hash") or ""),
            "handoff_schema_version": manifest.get("handoff_schema_version"),
            "source_identity": manifest.get("source_identity")
            or s0.get("source_identity"),
            "raw_ome_path": os.path.abspath(
                str(getattr(self.loader, "filepath", "") or "")),
        }

    def _fusion_settings_draft(self):
        """What the panel and the Intensity window say RIGHT NOW.

        This is what the previews draw. It is not what a segmentation search
        runs on: a search that re-read the live panel would change subject
        half-way through, and the user would have no way to tell which settings
        produced which result.
        """
        # Deep-copied, because the mapping this returns is the workbench's own
        # live dictionary: a snapshot holding a reference to it would keep
        # changing under the job that was given it, which is the whole thing a
        # snapshot exists to prevent.
        return {
            "fusion_config": copy.deepcopy(self._effective_fusion_config()),
            "display_mapping": copy.deepcopy(self._display_mapping() or {}),
        }

    def _fusion_settings_hash(self, draft=None):
        draft = self._fusion_settings_draft() if draft is None else draft
        return self._step1_config_hash(draft)

    def _committed_fusion_settings(self):
        """The snapshot the workers use, or None when nothing is committed."""
        return getattr(self, "_fusion_settings_snapshot", None)

    def _fusion_settings_dirty(self):
        """Does the screen show something the committed snapshot does not?"""
        snapshot = self._committed_fusion_settings()
        if not snapshot:
            return True
        return snapshot.get("hash") != self._fusion_settings_hash()

    def _fusion_settings_path(self, output_dir=None):
        out = output_dir or (self.step0_output or {}).get("step1_dir") \
            or (self.step0_output or {}).get("output_dir") or OUTPUT_DIR
        return os.path.join(out, "step1_fusion_settings.json")

    def _restore_fusion_settings(self):
        """Adopt the saved snapshot, but only if it is THIS handoff's.

        A snapshot names the manifest and the raw file it was frozen against.
        One from another dataset, or from a handoff that has since been
        republished, describes settings this session cannot reproduce, so it is
        left on disk and the draft simply counts as unsaved.
        """
        self._fusion_settings_snapshot = None
        path = self._fusion_settings_path()
        if not os.path.exists(path):
            self._update_fusion_settings_state()
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                snapshot = json.load(f) or {}
        except Exception as exc:                            # noqa: BLE001
            print(f"[Step1] saved fusion settings unreadable: {exc}")
            self._update_fusion_settings_state()
            return None
        # Fail closed. A missing field is not a wildcard: a snapshot that does
        # not say which handoff, which slide and which source it was frozen
        # against cannot be shown to be this session's, and adopting it would
        # let a search run on settings nobody can trace.
        def _refuse(why):
            print(f"[Step1] saved fusion settings not adopted: {why}")
            self._update_fusion_settings_state()
            return None

        if snapshot.get("version") != FUSION_SETTINGS_VERSION:
            return _refuse(f"version {snapshot.get('version')!r}, not "
                           f"{FUSION_SETTINGS_VERSION}")
        current = self._handoff_identity()
        saved = snapshot.get("handoff_identity")
        if current is None:
            return _refuse("no published handoff to compare against")
        if not isinstance(saved, dict) or not saved:
            return _refuse("it records no handoff identity")
        if not saved.get("source_identity") or not current.get("source_identity"):
            return _refuse("the source identity is missing on one side")
        for field in ("manifest_path", "manifest_digest",
                      "channel_remap_config_hash", "handoff_schema_version",
                      "raw_ome_path"):
            if not current.get(field) or not saved.get(field):
                return _refuse(f"{field} is missing on one side")
            if current[field] != saved[field]:
                return _refuse(f"{field} changed since it was saved")
        if self._step1_config_hash(current.get("source_identity")) != \
                self._step1_config_hash(saved.get("source_identity")):
            return _refuse("another source identity")
        # The stored hash is the file's own claim about itself; a file that was
        # edited would keep the old one. Recompute it from the content.
        stored = str(snapshot.get("hash") or "")
        actual = self._fusion_settings_hash({
            "fusion_config": snapshot.get("fusion_config"),
            "display_mapping": snapshot.get("display_mapping"),
        })
        if not stored or stored != actual:
            return _refuse("its contents do not match its hash")
        self._fusion_settings_snapshot = snapshot
        print(f"[Step1] fusion settings restored: {snapshot['hash'][:12]}")
        self._update_fusion_settings_state()
        return snapshot

    def _commit_fusion_settings(self, _checked=False):
        """Freeze the draft as the settings every job will run on.

        Fail-closed: if the file cannot be written the previous snapshot stands
        and the draft is still unsaved, because a search told it was saved and
        then run on something else is the confusion this exists to remove.
        """
        if self.loader is None:
            QMessageBox.warning(self, "Fusion settings",
                                "Load a dataset before saving fusion settings.")
            return False
        identity = self._handoff_identity()
        if identity is None:
            QMessageBox.warning(
                self, "Fusion settings",
                "There is no published Step0 handoff to save these settings "
                "against, so nothing was committed.")
            print("[Step1] fusion settings NOT saved: no handoff to bind to")
            self._update_fusion_settings_state()
            return False
        draft = self._fusion_settings_draft()
        snapshot = {
            "version": FUSION_SETTINGS_VERSION,
            "hash": self._fusion_settings_hash(draft),
            "fusion_config": draft["fusion_config"],
            "display_mapping": draft["display_mapping"],
            # WHAT the handoff said when these settings were frozen, not just
            # where it lives: the manifest keeps one path across republishes.
            "handoff_identity": identity,
            "source_identity": identity.get("source_identity"),
            "step0_manifest_path": identity.get("manifest_path", ""),
            "raw_ome_path": identity.get("raw_ome_path", ""),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        written, exc = self._write_fusion_settings(snapshot)
        if not written:
            QMessageBox.warning(
                self, "Fusion settings",
                f"The fusion settings could not be saved, so nothing was "
                f"committed.\n\nReason: {exc}")
            self._update_fusion_settings_state()
            return False
        self._fusion_settings_snapshot = snapshot
        print(f"[Step1] fusion settings committed: {snapshot['hash'][:12]}")
        self._update_fusion_settings_state()
        self._schedule_step1_session_save()
        return True

    def _write_fusion_settings(self, snapshot):
        """Put a snapshot on disk whole or not at all. Returns (ok, error)."""
        path = self._fusion_settings_path()
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as exc:                            # noqa: BLE001
            print(f"[Step1] fusion settings NOT written: {exc}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False, exc
        return True, None

    def _rebind_fusion_settings_to_handoff(self):
        """Follow the handoff the committed settings are bound to.

        A Save commits the display mapping first, and that republishes the
        manifest — same path, new contents. The settings the user saved a
        moment ago still name the manifest as it was, so the fusion about to be
        written would be refused by the next session's restore while this
        window went on saying "saved".

        The NUMBERS have not changed, so the hash does not either: a search that
        ran on these settings is still a search on these settings. Only the
        record of which handoff they are bound to moves, and it moves on disk
        and in memory together. Returns (ok, reason).
        """
        snapshot = self._committed_fusion_settings()
        if not snapshot:
            return True, "nothing committed"
        identity = self._handoff_identity()
        if identity is None:
            return False, "no published handoff to bind to"
        if identity == snapshot.get("handoff_identity"):
            return True, "unchanged"
        rebound = dict(snapshot)
        rebound["handoff_identity"] = identity
        rebound["source_identity"] = identity.get("source_identity")
        rebound["step0_manifest_path"] = identity.get("manifest_path", "")
        rebound["raw_ome_path"] = identity.get("raw_ome_path", "")
        rebound["rebound_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        written, exc = self._write_fusion_settings(rebound)
        if not written:
            # Memory and disk must not disagree about which handoff these
            # settings belong to, so neither keeps the claim.
            self._forget_fusion_settings("the rebind could not be written")
            return False, str(exc)
        self._fusion_settings_snapshot = rebound
        print(f"[Step1] fusion settings rebound to the republished handoff "
              f"({rebound['hash'][:12]} unchanged)")
        self._update_fusion_settings_state()
        return True, "rebound"

    def _forget_fusion_settings(self, reason=""):
        """Another dataset, or a handoff that no longer holds: the snapshot
        described settings for a slide that is no longer on screen."""
        if getattr(self, "_fusion_settings_snapshot", None) is not None:
            print(f"[Step1] fusion settings dropped{': ' + reason if reason else ''}")
        self._fusion_settings_snapshot = None
        self._update_fusion_settings_state()

    def _update_fusion_settings_state(self):
        """Say, on screen, whether what is drawn is what a search would run."""
        dirty = self._fusion_settings_dirty()
        label = getattr(self, "_fusion_settings_label", None)
        if label is not None:
            if dirty:
                label.setText("Unsaved fusion changes")
                label.setStyleSheet("color:#f4c45e;font-size:10px;")
            else:
                snapshot = self._committed_fusion_settings() or {}
                label.setText(f"Fusion settings saved · {snapshot.get('hash', '')[:8]}")
                label.setStyleSheet("color:#6bffa0;font-size:10px;")
        button = getattr(self, "_btn_save_fusion_settings", None)
        if button is not None:
            button.setEnabled(dirty)
        return dirty

    def _require_committed_fusion_settings(self, what):
        """Refuse to start `what` while the screen and the snapshot disagree."""
        if not self._fusion_settings_dirty():
            return True
        QMessageBox.warning(
            self, "Unsaved fusion changes",
            f"{what} runs on the saved fusion settings, and the channel "
            "settings on screen have not been saved.\n\nClick "
            "\"Save Fusion Settings\" first — the preview keeps following "
            "what you change, and a running job keeps the settings it started "
            "with.")
        print(f"[Step1] {what} refused: fusion settings are unsaved")
        return False

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

    def _set_intensity_editing_enabled(self, enabled):
        """Lock or release the ONE Intensity window for a running job.

        A LOCK, not a step policy: what may not happen during a fusion run is
        an edit landing while the worker writes from a frozen copy of the
        configuration. Which step the user is looking at has nothing to do
        with it -- Min/Max/Gamma are editable in Step0, Step1, Step2 and
        Step3 alike.
        """
        if enabled:
            self._display.unlock_intensity()
        else:
            self._display.lock_intensity("a fusion run is using a frozen "
                                         "copy of the configuration")

    def _lock_ui(self):
        """Disable all interactive elements during fusion."""
        # The mapping was frozen a moment ago; letting it be edited while the
        # worker fuses with the frozen copy would put the screen and the file
        # back out of step for the length of the run.
        self._set_intensity_editing_enabled(False)
        self.btn_save.setEnabled(False)
        self.config.setEnabled(False)
        self.search.setEnabled(False)
        self._btn_back_to_step0.setEnabled(False)

    def _unlock_ui(self):
        """Re-enable UI after fusion completes or errors."""
        self._set_intensity_editing_enabled(True)
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
        d = getattr(self, "_fusion_dialog", None)
        if d is not None:
            d.setValue(pct)
            d.setLabelText(msg)

    def _on_fusion_done(self, zarr_path):
        self._close_fusion_dialog()
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
            "segmentation_param_path": getattr(self, "_pending_segmentation_param_path", ""),
            "roi_info": self._rois if self._rois else [],
            "output_dir": OUTPUT_DIR,
            "step2_dir": (self.step0_output or {}).get("step2_dir", ""),
            "roi_id": (self.step0_output or {}).get("roi_id", ""),
            "roi_dir": (self.step0_output or {}).get("roi_dir", ""),
            "ome_tiff_path": OME_TIFF_FILE,
        }
        if self.step1_output.get("roi_id") and self.step0_output.get("project_output_dir"):
            try:
                mark_roi_step(self.step0_output["project_output_dir"], self.step1_output["roi_id"], "step1", "done")
            except Exception:
                print(f"[Step1] failed to update ROI step1 status:\n{traceback.format_exc()}")
        self.step1_done = True
        self._update_next_button()
        if (
            not is_wholecell
            and getattr(self, "_pending_dapi_input_meta", None)
            and not getattr(self, "_reused_dapi_input_meta", False)
        ):
            self._write_dapi_input_meta(self._pending_dapi_input_meta)
        if (
            is_wholecell
            and getattr(self, "_pending_fused_zarr_meta", None)
            and not getattr(self, "_reused_fused_zarr_meta", False)
        ):
            self._write_fused_zarr_meta(self._pending_fused_zarr_meta, zarr_path)
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
        self._close_fusion_dialog()
        self._fusion_lbl.setText(f"✗  Fusion error — see terminal for details")
        self._unlock_ui()
        QMessageBox.critical(self, "Fusion Error", msg)
        print(f"[Fusion Error]\n{msg}")

    def _dapi_input_meta_path(self):
        return os.path.join(OUTPUT_DIR, "dapi_input_meta.json")

    def _expected_dapi_input_meta(self, worker_fcfg, selected_method):
        active_roi = self._active_roi or (self._rois[0] if self._rois else None)
        regions = []
        if self._rois:
            for roi in self._rois:
                bbox = [int(v) for v in roi.get("bbox_fullres", [])]
                if len(bbox) == 4:
                    regions.append({
                        "roi_name": roi.get("name") or roi.get("display_name") or "",
                        "roi_id": (self.step0_output or {}).get("roi_id", ""),
                        "roi_bbox": bbox,
                        "shape": [bbox[1] - bbox[0], bbox[3] - bbox[2], 2],
                    })
        else:
            h, w = self.loader.shape
            regions.append({
                "roi_name": "full",
                "roi_id": "",
                "roi_bbox": [0, h, 0, w],
                "shape": [h, w, 2],
            })
        source_path = (
            self._corrected_zarr_path
            or (self.step0_output or {}).get("corrected_zarr_path")
            or getattr(self.loader, "filepath", "")
        )
        meta = {
            "version": 1,
            "purpose": "step1_dapi_input_zarr",
            "method": selected_method,
            "source_path": os.path.abspath(source_path) if source_path else "",
            "raw_ome_path": os.path.abspath(getattr(self.loader, "filepath", OME_TIFF_FILE)),
            "roi_id": (self.step0_output or {}).get("roi_id", ""),
            "roi_bbox": active_roi.get("bbox_fullres") if active_roi else None,
            "regions": regions,
            "dapi_channel": worker_fcfg.get("nucleus", {}).get("channel"),
            "shape": regions[0]["shape"] if len(regions) == 1 else [r["shape"] for r in regions],
            "dtype": "uint16",
            "resolution": worker_fcfg.get("resolution") or None,
            "normalization_source": "corrected" if self._corrected_zarr_path else "raw",
            "background_correction_source": os.path.abspath(self._corrected_zarr_path) if self._corrected_zarr_path else "",
            "pixel_size": (self.step0_output or {}).get("pixel_size"),
            "artifact_kind": "step1_dapi_input_zarr",
            "fusion_formula_version": FUSION_FORMULA_VERSION,
            # Channel 0 of this file is a full marker fusion — Mesmer's fused
            # mode reads it as the membrane channel — so the weights that made
            # it belong in this identity too. They were absent, which is why a
            # weight change did not invalidate a DAPI-input zarr.
            "fusion_config": worker_fcfg,
            "display_mapping": self._display_mapping_identity(worker_fcfg),
            "code_version": "block01_step1_v9_dapi_meta",
        }
        # Fingerprint the DAPI channel's Step0 manual remap so that changing (or
        # clearing) the remap invalidates the reused DAPI input zarr — otherwise a
        # stale, un-remapped DAPI input is served. Key is added ONLY when the DAPI
        # channel actually carries a remap, so legacy metas (and no-remap runs,
        # which never had the key) still compare equal and are not needlessly
        # regenerated.
        dapi_ch = meta.get("dapi_channel")
        dapi_remap = (self._load_step0_remap_params()[0] or {}).get(dapi_ch) if dapi_ch else None
        if dapi_remap:
            meta["channel_remap_hash"] = self._remap_params_hash({dapi_ch: dapi_remap})
        # The store stamps this into itself, so a finished DAPI-input zarr can
        # be told apart from a truncated one at the same path.
        meta["config_hash"] = self._step1_config_hash(meta)
        return meta

    def _display_mapping_identity(self, worker_fcfg):
        """What every pixel of this artifact was mapped through.

        The fusion weights say how channels are combined; this says what each
        channel's [0,1] signal MEANT before they were combined. A run whose
        Min/Max/Gamma changed is a different result even at identical weights,
        so it belongs in the identity rather than outside it.
        """
        params = dict((worker_fcfg or {}).get("channel_remap_params") or {})
        if not params:
            params = self._display_mapping()
        return {
            "source": os.path.abspath(self._load_step0_remap_params()[1] or ""),
            "hash": self._remap_params_hash(params or {}),
        }

    @staticmethod
    def _remap_params_hash(params):
        payload = json.dumps(params, sort_keys=True, default=str)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _dapi_meta_compare_view(meta):
        keys = (
            "source_path", "raw_ome_path", "roi_id", "roi_bbox", "regions",
            "dapi_channel", "shape", "dtype", "resolution",
            "normalization_source", "background_correction_source", "pixel_size",
            "code_version", "channel_remap_hash",
            "artifact_kind", "fusion_formula_version", "fusion_config",
            "display_mapping",
        )
        return {k: meta.get(k) for k in keys}

    def _existing_dapi_zarr_path(self):
        meta_path = os.path.join(OUTPUT_DIR, "fusion_meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                regions = meta.get("regions") or []
                if regions:
                    path = regions[0].get("zarr_path")
                    if path and os.path.exists(path):
                        return path
            except Exception:
                pass
        if self._rois:
            name = self._rois[0].get("name", "ROI_1")
            path = os.path.join(OUTPUT_DIR, f"fused_{name}.zarr")
        else:
            path = os.path.join(OUTPUT_DIR, "fused.zarr")
        return path if os.path.exists(path) else ""

    def _fusion_meta_path(self):
        return os.path.join(OUTPUT_DIR, "fusion_meta.json")

    @staticmethod
    def _canonical_step1_config_value(value):
        transient = {
            "generated_at", "created_at", "saved_at", "last_used",
            "runtime_seconds", "avg_tile_s", "tile_seconds",
            "preview_path", "config_hash", "old_hash", "new_hash",
        }
        if isinstance(value, dict):
            return {
                str(k): MainWindow._canonical_step1_config_value(v)
                for k, v in sorted(value.items(), key=lambda item: str(item[0]))
                if str(k) not in transient
            }
        if isinstance(value, (list, tuple)):
            return [MainWindow._canonical_step1_config_value(v) for v in value]
        if isinstance(value, float):
            return round(value, 6)
        if isinstance(value, np.floating):
            return round(float(value), 6)
        if isinstance(value, np.integer):
            return int(value)
        return value

    @classmethod
    def _step1_config_hash(cls, value):
        canonical = cls._canonical_step1_config_value(value)
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _expected_fused_zarr_meta(self, worker_fcfg, selected_method):
        active_roi = self._active_roi or (self._rois[0] if self._rois else None)
        regions = []
        if self._rois:
            for roi in self._rois:
                bbox = [int(v) for v in roi.get("bbox_fullres", [])]
                if len(bbox) == 4:
                    regions.append({
                        "roi_name": roi.get("name") or roi.get("display_name") or "",
                        "roi_id": (self.step0_output or {}).get("roi_id", ""),
                        "roi_bbox": bbox,
                        "shape": [bbox[1] - bbox[0], bbox[3] - bbox[2], 2],
                    })
        else:
            h, w = self.loader.shape
            regions.append({
                "roi_name": "full",
                "roi_id": "",
                "roi_bbox": [0, h, 0, w],
                "shape": [h, w, 2],
            })
        source_path = (
            self._corrected_zarr_path
            or (self.step0_output or {}).get("corrected_zarr_path")
            or getattr(self.loader, "filepath", "")
        )
        meta = {
            "version": 1,
            "purpose": "step1_fused_zarr",
            "method": selected_method,
            "source_path": os.path.abspath(source_path) if source_path else "",
            "raw_ome_path": os.path.abspath(getattr(self.loader, "filepath", OME_TIFF_FILE)),
            "roi_id": (self.step0_output or {}).get("roi_id", ""),
            "roi_bbox": active_roi.get("bbox_fullres") if active_roi else None,
            "regions": regions,
            "shape": regions[0]["shape"] if len(regions) == 1 else [r["shape"] for r in regions],
            "dtype": "uint16",
            "resolution": worker_fcfg.get("resolution") or None,
            "normalization_source": "corrected" if self._corrected_zarr_path else "raw",
            "background_correction_source": os.path.abspath(self._corrected_zarr_path) if self._corrected_zarr_path else "",
            "pixel_size": (self.step0_output or {}).get("pixel_size"),
            "fusion_config": worker_fcfg,
            # What this file IS. The whole-cell and the DAPI-input runs write the
            # same `fused_<roi>.zarr` filename, so without this a run of one kind
            # could be picked up as the other.
            "artifact_kind": "step1_fused_zarr",
            # Which arithmetic made these pixels.
            "fusion_formula_version": FUSION_FORMULA_VERSION,
            "display_mapping": self._display_mapping_identity(worker_fcfg),
            "code_version": "block01_step1_v12_fused_reuse",
        }
        meta["config_hash"] = self._step1_config_hash(meta)
        return meta

    def _existing_fused_zarr_path(self):
        meta_path = self._fusion_meta_path()
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                regions = meta.get("regions") or []
                if regions:
                    path = regions[0].get("zarr_path")
                    if path and os.path.isdir(path):
                        return path
            except Exception:
                pass
        if self._rois:
            name = self._rois[0].get("name", "ROI_1")
            path = os.path.join(OUTPUT_DIR, f"fused_{name}.zarr")
        else:
            path = os.path.join(OUTPUT_DIR, "fused.zarr")
        return path if os.path.isdir(path) else ""

    @staticmethod
    def _is_valid_existing_fused_zarr(path):
        if not path or not os.path.isdir(path):
            return False
        try:
            z = zarr.open(path, mode="r")
            shape = getattr(z, "shape", None)
            if shape is not None and len(shape) >= 3 and int(shape[-1]) == 2:
                return True
            attrs = dict(getattr(z, "attrs", {}) or {})
            if attrs.get("cellpose_channels") == [1, 2] or attrs.get("channel_1") == "nucleus":
                return True
            for key in ("fused", "data", "0"):
                if key in z:
                    arr = z[key]
                    arr_shape = getattr(arr, "shape", None)
                    if arr_shape is not None and len(arr_shape) >= 3 and int(arr_shape[-1]) == 2:
                        return True
        except Exception:
            return False
        return False

    def _write_fused_zarr_meta(self, expected_meta, zarr_path=""):
        meta_path = self._fusion_meta_path()
        meta = {}
        try:
            if os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update({
            "purpose": expected_meta.get("purpose"),
            "method": expected_meta.get("method"),
            "source_path": expected_meta.get("source_path"),
            "raw_ome_path": expected_meta.get("raw_ome_path"),
            "roi_id": expected_meta.get("roi_id"),
            "roi_bbox": expected_meta.get("roi_bbox"),
            "shape": expected_meta.get("shape"),
            "dtype": expected_meta.get("dtype"),
            "resolution": expected_meta.get("resolution"),
            "normalization_source": expected_meta.get("normalization_source"),
            "background_correction_source": expected_meta.get("background_correction_source"),
            "pixel_size": expected_meta.get("pixel_size"),
            "fusion_config": expected_meta.get("fusion_config"),
            "artifact_kind": expected_meta.get("artifact_kind"),
            "fusion_formula_version": expected_meta.get("fusion_formula_version"),
            "display_mapping": expected_meta.get("display_mapping"),
            "config_hash": expected_meta.get("config_hash"),
            "last_used": time.strftime("%Y-%m-%d %H:%M:%S"),
            "code_version": expected_meta.get("code_version"),
        })
        if not meta.get("regions"):
            regions = []
            for region in expected_meta.get("regions") or []:
                item = {
                    "roi_name": region.get("roi_name", "full"),
                    "zarr_path": zarr_path or self._existing_fused_zarr_path(),
                    "zarr_shape": region.get("shape"),
                    "bbox": region.get("roi_bbox"),
                }
                regions.append(item)
            meta["regions"] = regions
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except Exception:
            print(f"[Step1] failed to write fusion_meta.json:\n{traceback.format_exc()}")

    def _all_regions_reusable(self, expected_meta, old_meta):
        """Every region of a multi-ROI result has to be there and be current.

        The reuse decision used to look at the first zarr the meta names. With
        several ROIs that meant an intact ROI_1 vouched for the whole batch,
        while ROI_2 could be missing, truncated or left over from another
        configuration.
        """
        expected_regions = list(expected_meta.get("regions") or [])
        old_regions = list(old_meta.get("regions") or [])
        if not expected_regions:
            return True
        if len(old_regions) != len(expected_regions):
            print(f"[Step1] the existing result covers {len(old_regions)} region(s), "
                  f"not {len(expected_regions)}; regenerating")
            return False
        by_name = {str(r.get("roi_name") or ""): r for r in old_regions}
        seen_paths = {}
        for i, want in enumerate(expected_regions):
            name = str(want.get("roi_name") or "")
            have = by_name.get(name)
            if have is None:
                if name and any(str(r.get("roi_name") or "") for r in old_regions):
                    # The recorded regions are named and none of them is this
                    # one: matching by position here is how ROI_1's file came to
                    # stand in for ROI_2.
                    print(f"[Step1] the existing result has no region named "
                          f"{name}; regenerating")
                    return False
                have = old_regions[i]
            path = str(have.get("zarr_path") or "")
            if not path:
                # The DAPI-input meta records regions without paths; the worker
                # names them by ROI, so derive the name it would have written.
                path = os.path.join(
                    OUTPUT_DIR,
                    f"fused_{name}.zarr" if name and name != "full" else "fused.zarr")
            label = f"region {name or i + 1}"
            real = os.path.realpath(path)
            if real in seen_paths:
                print(f"[Step1] {label} and region {seen_paths[real]} name the "
                      "same store; regenerating")
                return False
            seen_paths[real] = name or i + 1
            if not self._is_valid_existing_fused_zarr(path):
                print(f"[Step1] {label} is missing or unreadable at {path}; "
                      "regenerating")
                return False
            want_shape = [int(v) for v in (want.get("shape") or [])]
            have_shape = [int(v) for v in (have.get("zarr_shape") or [])]
            if want_shape and have_shape and want_shape != have_shape:
                print(f"[Step1] {label} has shape {have_shape}, not {want_shape}; "
                      "regenerating")
                return False
            if not self._body_matches_expected(path, expected_meta, label):
                return False
            # The store's own account of which region it covers. Every ROI of
            # one run shares a config hash, so the hash alone cannot tell them
            # apart; the pixels' extent can.
            if not self._body_is_region(path, want, label):
                return False
        return True

    def _body_is_region(self, path, want, label):
        """Does the store itself say it covers the region we expect?

        A store made by the current formula records which ROI it holds and the
        area it covers, so for those fields "not stated" is a mismatch, not a
        pass: every ROI of one run shares a config hash, and the extent of the
        pixels is the only thing that tells them apart.
        """
        try:
            z = zarr.open(path, mode="r")
            attrs = dict(getattr(z, "attrs", {}) or {})
            real_shape = [int(v) for v in (getattr(z, "shape", None) or [])]
        except Exception as exc:
            print(f"[Step1] {label} could not be read: {exc}; regenerating")
            return False
        want_shape = [int(v) for v in (want.get("shape") or [])]
        if want_shape and real_shape and real_shape != want_shape:
            print(f"[Step1] {label} is {real_shape} on disk, not {want_shape}; "
                  "regenerating")
            return False
        current = attrs.get("fusion_formula_version") == FUSION_FORMULA_VERSION
        want_name = str(want.get("roi_name") or "")
        body_name = str(attrs.get("roi_name") or "")
        if want_name and not body_name and current:
            print(f"[Step1] {label} does not say which ROI it holds; regenerating")
            return False
        if want_name and body_name and body_name != want_name:
            print(f"[Step1] {label} holds {body_name}, not {want_name}; "
                  "regenerating")
            return False
        want_bbox = [int(v) for v in (want.get("roi_bbox") or [])]
        body_bbox = [int(v) for v in (attrs.get("bbox_fullres") or [])]
        if want_bbox and not body_bbox and current:
            print(f"[Step1] {label} does not say which area it covers; regenerating")
            return False
        if want_bbox and body_bbox and body_bbox != want_bbox:
            print(f"[Step1] {label} covers {body_bbox}, not {want_bbox}; "
                  "regenerating")
            return False
        return True

    def _expected_region_for_active_roi(self, expected_meta):
        """The one region a single stored path is supposed to hold."""
        regions = list(expected_meta.get("regions") or [])
        if not regions:
            return None
        active = self._active_roi or (self._rois[0] if self._rois else None)
        name = str((active or {}).get("name")
                   or (active or {}).get("display_name") or "")
        if name:
            for region in regions:
                if str(region.get("roi_name") or "") == name:
                    return region
            return None
        return regions[0]

    def _expected_meta_for_current_config(self, method=""):
        """The identity a fusion of what is loaded right now would have.

        Built the way `_save` builds it, so a stored result can be compared
        against the configuration in front of the user rather than merely
        against "some Step1 output of the current formula version".
        Returns None when there is not enough loaded to say.
        """
        if self.loader is None:
            return None
        method = str(method or self._active_segmentation_method
                     or (self._p2_params or {}).get("method")
                     or CELLPOSE_WHOLECELL_FUSION)
        try:
            fcfg = self._effective_fusion_config()
            fcfg.update({
                "ome_tiff": OME_TIFF_FILE,
                "output_dir": OUTPUT_DIR,
                "norm_low": NORM_LOW,
                "norm_high": NORM_HIGH,
                "channel_remap_params": self._load_step0_remap_params()[0],
            })
            if method == CELLPOSE_WHOLECELL_FUSION:
                return self._expected_fused_zarr_meta(fcfg, method)
            nuc_ch = (fcfg.get("nucleus") or {}).get("channel") \
                or self.config.nucleus_channel()
            fcfg["nucleus"] = {"channel": nuc_ch, "weight": 1.0}
            return self._expected_dapi_input_meta(fcfg, method)
        except Exception as exc:
            print(f"[Step1] could not describe the current fusion identity: {exc}")
            return None

    def _restorable_fused_zarr(self, path):
        """The session's fused zarr, only if it is still usable as one.

        A session file records a path, and Step2 is handed that path as a
        finished result. Restoring it unchecked was a way around every gate the
        Save path applies: a store made by the old formula, a DAPI-input store
        at the whole-cell name, a run that was cancelled half-way, or a path
        that no longer exists at all could all come back as "Step1 done".
        """
        path = str(path or "")
        if not path:
            return ""
        # A run killed between moving the old store aside and moving the new one
        # in leaves the previous result under `.previous`. Recovering it only at
        # the start of the next fusion would mean the user opens the project and
        # is told Step1 is not done, with the result sitting right there.
        try:
            FullFusionWorker._recover_interrupted_publish(path)
        except Exception as exc:
            print(f"[Step1] could not recover an interrupted publish: {exc}")
        if not os.path.isdir(path) or not self._is_valid_existing_fused_zarr(path):
            print(f"[Step1] session fused zarr missing or unreadable: {path}")
            return ""
        attrs = self._fused_zarr_body_identity(path)
        if attrs is None:
            print(f"[Step1] session fused zarr is not marked complete: {path}")
            return ""
        # And it has to be THIS configuration's result, not merely a result of
        # the current formula: the weights, the display mapping, the ROI and the
        # method all decide what those pixels are. A store of the other kind
        # fails here too, because the expected kind follows from the method.
        expected = self._expected_meta_for_current_config()
        if expected is None:
            print("[Step1] cannot describe the current fusion; not restoring "
                  f"{path}")
            return ""
        if not self._body_matches_expected(path, expected, "the session fused zarr"):
            return ""
        # Which ROI those pixels are. One run writes every ROI with the same
        # config hash, so without this a session could hand Step2 the fusion of
        # a neighbouring region and nothing would notice.
        want = self._expected_region_for_active_roi(expected)
        if want is None:
            print("[Step1] the session result names no region of this ROI; "
                  f"not restoring {path}")
            return ""
        if not self._body_is_region(path, want, "the session fused zarr"):
            return ""
        return path

    @staticmethod
    def _fused_zarr_body_identity(path):
        """What the store itself says it is, or None if it will not say.

        The sidecar describes a path, not a file: a run that was cancelled or
        crashed part-way left a store of the right shape at that path with the
        previous run's meta still beside it. Only the store's own attrs,
        written last, can tell a finished result from a truncated one.
        """
        try:
            attrs = dict(getattr(zarr.open(path, mode="r"), "attrs", {}) or {})
        except Exception as exc:
            print(f"[Step1] could not read the fused zarr attrs: {exc}")
            return None
        if attrs.get("complete") is not True:
            return None
        return attrs

    def _body_matches_expected(self, path, expected_meta, label):
        """Fail closed unless the store itself matches the identity we want."""
        attrs = self._fused_zarr_body_identity(path)
        if attrs is None:
            print(f"[Step1] {label} at {path} is not marked complete; regenerating")
            return False
        kind = str(attrs.get("artifact_kind") or "")
        want_kind = str(expected_meta.get("artifact_kind") or "")
        if kind != want_kind:
            print(f"[Step1] {label} says it is {kind or 'an unknown kind'}, "
                  f"not {want_kind}; regenerating")
            return False
        if attrs.get("fusion_formula_version") != expected_meta.get(
                "fusion_formula_version"):
            print(f"[Step1] {label} was made by formula "
                  f"{attrs.get('fusion_formula_version')!r}, not "
                  f"{expected_meta.get('fusion_formula_version')!r}; regenerating")
            return False
        body_hash = str(attrs.get("config_hash") or "")
        want_hash = str(expected_meta.get("config_hash") or "")
        if not body_hash or body_hash != want_hash:
            print(f"[Step1] {label} carries config hash {body_hash or 'none'}, "
                  f"not {want_hash or 'none'}; regenerating")
            return False
        return True

    def _try_reuse_fused_zarr(self, expected_meta):
        existing_zarr = self._existing_fused_zarr_path()
        if bool(getattr(self, "_force_dapi_zarr", None) and self._force_dapi_zarr.isChecked()):
            print("[Step1] force overwrite enabled, regenerating fused zarr")
            return False
        if not existing_zarr or not self._is_valid_existing_fused_zarr(existing_zarr):
            return False

        print(f"[Step1] existing fused zarr found: {existing_zarr}")
        meta_path = self._fusion_meta_path()
        old_meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    old_meta = json.load(f) or {}
            except Exception as e:
                print(f"[Step1] failed to read fusion_meta.json: {e}")
                old_meta = {}
        old_hash = str(old_meta.get("config_hash") or "")
        new_hash = str(expected_meta.get("config_hash") or "")

        # Fail closed. A file that cannot say what made it is not evidence that
        # it matches: an unreadable meta, a missing hash, a missing formula
        # version and a run of the other kind all used to end in "reuse it
        # anyway", which is how a DAPI-input run's zarr could be served as the
        # whole-cell fusion.
        kind = str(old_meta.get("artifact_kind") or "")
        if kind != expected_meta.get("artifact_kind"):
            print(f"[Step1] existing fused zarr was written as {kind or 'an unknown kind'}, "
                  f"not {expected_meta.get('artifact_kind')}; regenerating")
            return False
        if old_meta.get("fusion_formula_version") != expected_meta.get(
                "fusion_formula_version"):
            print("[Step1] existing fused zarr was made by another fusion formula "
                  f"({old_meta.get('fusion_formula_version')!r} vs "
                  f"{expected_meta.get('fusion_formula_version')!r}); regenerating")
            return False
        if not old_hash:
            print("[Step1] existing fused zarr has no config hash; regenerating")
            return False
        if old_hash == new_hash:
            if not self._body_matches_expected(existing_zarr, expected_meta,
                                               "the existing fused zarr"):
                return False
            if not self._all_regions_reusable(expected_meta, old_meta):
                return False
            print("[Step1] config unchanged, skip generation")
            self._write_fused_zarr_meta(expected_meta, existing_zarr)
            self._reused_fused_zarr_meta = True
            self._on_fusion_done(existing_zarr)
            print("[Step1] Next unlocked")
            return True

        print("[Step1] fused zarr exists but config changed")
        print(f"[Step1] old_hash={old_hash}")
        print(f"[Step1] new_hash={new_hash}")
        print("[Step1] regenerating fused zarr")
        return False

    def _try_reuse_dapi_input_zarr(self, expected_meta):
        meta_path = self._dapi_input_meta_path()
        existing_zarr = self._existing_dapi_zarr_path()
        if bool(getattr(self, "_force_dapi_zarr", None) and self._force_dapi_zarr.isChecked()):
            reason = "force_regenerate_dapi_zarr checked"
        elif not existing_zarr:
            reason = "no existing DAPI input zarr"
        elif not os.path.exists(meta_path):
            reason = "dapi_input_meta.json missing"
        else:
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    old_meta = json.load(f) or {}
                if str(old_meta.get("artifact_kind") or "") != expected_meta.get("artifact_kind"):
                    raise _ArtifactKindMismatch(
                        old_meta.get("artifact_kind") or "an unknown kind")
                if (self._dapi_meta_compare_view(old_meta)
                        == self._dapi_meta_compare_view(expected_meta)
                        and self._body_matches_expected(
                            existing_zarr, expected_meta,
                            "the existing DAPI input zarr")
                        and self._all_regions_reusable(expected_meta, old_meta)):
                    print(f"[Step1] DAPI input zarr meta: {json.dumps(expected_meta, indent=2, default=str)}")
                    print("[Step1] DAPI input zarr reuse/regenerate reason: metadata match")
                    print("Reusing existing DAPI input zarr")
                    self._reused_dapi_input_meta = True
                    self._on_fusion_done(existing_zarr)
                    return True
                reason = "metadata changed"
            except _ArtifactKindMismatch as e:
                reason = f"existing zarr was written as {e}"
            except Exception as e:
                reason = f"failed to read existing meta: {e}"
        print(f"[Step1] DAPI input zarr meta: {json.dumps(expected_meta, indent=2, default=str)}")
        print(f"[Step1] DAPI input zarr reuse/regenerate reason: {reason}")
        if existing_zarr and reason != "force_regenerate_dapi_zarr checked":
            answer = QMessageBox.question(
                self,
                "Regenerate DAPI input zarr?",
                "Existing DAPI input zarr metadata does not match the current source/ROI/DAPI settings.\n\n"
                f"Reason: {reason}\n\n"
                "Regenerate and overwrite the DAPI input zarr?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                print("[Step1] DAPI input zarr reuse/regenerate reason: user cancelled overwrite")
                return True
        return False

    def _write_dapi_input_meta(self, expected_meta):
        meta = dict(expected_meta)
        meta["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self._dapi_input_meta_path(), "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            print(f"[Step1] DAPI input zarr meta: {json.dumps(meta, indent=2, default=str)}")
        except Exception:
            print(f"[Step1] failed to write dapi_input_meta.json:\n{traceback.format_exc()}")

    def _preview_channel_signal(self, ch, arr, remap):
        """One channel's [0,1] signal for the Step1 fusion PREVIEW, cached.

        A thin delegate, for the same reason as `_overlay_gray`: one
        implementation in `core.preview_compose`, two callers -- this window's
        immediate redraws and the background frame worker.
        """
        return preview_compose.channel_signal(
            self._preview_patch_idx, ch, arr, remap, self._signal_cache,
            self.loader._norm, span=perf_trace.span)

    # ── Save ────────────────────────────────────────────────────────

    def _commit_display_mapping_for_save(self):
        """Freeze and publish the mapping this Save is about to fuse with.

        The fusion config written moments later reads the COMMITTED file, so
        after this returns the numbers on screen, the numbers in the config and
        the numbers in the manifest hash are one set.
        """
        step0 = getattr(self, "_step0", None)
        if step0 is None or not hasattr(step0, "commit_display_mapping"):
            return True, "no step0 page"
        required = list(self._fusion_weighted_channels())
        channels = list(required)
        for ch in self.config.visible_channels():
            if ch not in channels:
                channels.append(ch)
        try:
            return step0.commit_display_mapping(channels, required=required)
        except Exception as exc:
            print(f"[Step1] display-mapping commit raised: {exc}")
            return False, f"commit raised: {exc}"

    def _display_mapping(self, channels=None, blocking=True,
                         resident_only=False, computed_only=False):
        """The Min/Max/Gamma Step1 should DRAW with, for the channels asked for.

        The draft: what the Intensity window is showing the user right now.
        Nothing is written until a Save commits it, so a slider that has moved
        is visible here immediately and on disk not at all — which is exactly
        why a Save commits the draft before it fuses anything, and why an
        uncommitted draft makes an existing result unreusable.

        Falls back to the committed file when there is no draft (no workbench
        engaged yet), so a freshly opened project still draws with the mapping
        its handoff carries.

        `resident_only=True` exists for callers that must not wait on pixels,
        and the render paths deliberately do NOT use it yet: a frame that skips a
        channel's window draws that channel with a provisional percentile
        instead, and the screen would stop matching what a Save writes --
        the one equality this preview exists to keep. Scoping WHICH channels
        are asked for is what makes the wait affordable: one channel's window
        is computed once and cached, where 29 were computed per first frame.

        `channels` is NOT optional in the render paths, and this is why:
        called with nothing, `display_mapping_for_preview()` completes the
        draft for every channel the loader has, computing an automatic window
        for each one it has never seen -- from whole-slide pixels, on the GUI
        thread. MEASURED on the real desk: the first frame after a patch was
        drawn showed DAPI alone and took 7493.66 ms, of which the remap it
        actually drew was 3.98 ms and the composite 19.65 ms; the other 7.47
        seconds were spent seeding windows for the 28 channels that frame did
        not contain, while the window answered nothing and the second patch
        the user had already drawn sat in the event queue. A frame asks for
        the channels it draws.
        """
        step0 = getattr(self, "_step0", None)
        draft = {}
        if step0 is not None and hasattr(step0, "display_mapping_for_preview"):
            try:
                draft = step0.display_mapping_for_preview(
                    channels=channels, blocking=blocking,
                    resident_only=resident_only,
                    computed_only=computed_only) or {}
            except Exception as exc:
                print(f"[Step1] could not read the display mapping: {exc}")
                draft = {}
        if draft:
            return draft
        return self._load_step0_remap_params()[0]

    def _load_step0_remap_params(self):
        """Load the Step0 Channel Remap config (if any) as {channel: params}.

        Looks in the ROI step0 dir first (canonical <roi>/step0/
        step0_channel_remap.json), then the legacy step1_5/channel_remap_configs
        location. Returns ({}, "") when none is found."""
        try:
            from ..utils.channel_remap_config import load_channel_remap_config
        except Exception:
            return {}, ""
        cands = []
        s0 = self.step0_output or {}
        # Schema-v2 handoffs carry the canonical path and semantic hash.  This
        # is the normal path: do not consult the Step0 widget or legacy folders,
        # even when the declared file is absent.  A missing file means this
        # handoff has no remap config, not that another ROI's config is suitable.
        if int(s0.get("handoff_schema_version", 1) or 1) >= 2:
            declared_raw = s0.get("channel_remap_config_path") or ""
            if not declared_raw:
                return {}, ""
            declared = os.path.abspath(declared_raw)
            if not os.path.exists(declared):
                return {}, ""
            expected_hash = str(s0.get("channel_remap_config_hash") or "")
            try:
                from ..utils.channel_remap_config import channel_remap_config_hash
                cfg = load_channel_remap_config(declared)
                actual_hash = channel_remap_config_hash(cfg)
                if expected_hash and actual_hash != expected_hash:
                    print(f"[Step1] remap hash mismatch for handoff: expected={expected_hash} actual={actual_hash}")
                    return {}, ""
                chans = cfg.get("channels") or {}
                params = {str(n): dict(pr) for n, pr in chans.items()}
                return params, declared
            except Exception as exc:
                print(f"[Step1] remap config load failed {declared}: {exc}")
                return {}, ""

        # Explicit legacy compatibility only.  Older manifests did not declare
        # a canonical remap path; their fallback is intentionally isolated.
        st0 = getattr(self, "_step0", None)
        last = getattr(st0, "_last_saved_remap_path", "") if st0 is not None else ""
        if last:
            cands.append(last)
        if st0 is not None and hasattr(st0, "_step0_conditioning_config_path"):
            try:
                cands.append(st0._step0_conditioning_config_path())
            except Exception:
                pass
        if s0.get("step0_dir"):
            cands.append(os.path.join(s0["step0_dir"], "step0_channel_remap.json"))
        out = s0.get("output_dir") or s0.get("project_output_dir") or OUTPUT_DIR
        cands.append(os.path.join(out, "step1_5", "channel_remap_configs",
                                  "step0_channel_remap.json"))
        seen = set()
        for p in cands:
            if not p or p in seen:
                continue
            seen.add(p)
            if os.path.exists(p):
                # mtime cache: the preview calls this on every redraw; avoid
                # re-reading the JSON unless the file changed.
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    mt = None
                cached = getattr(self, "_remap_cache", None)
                if cached and cached[0] == p and cached[1] == mt:
                    return cached[2], p
                try:
                    cfg = load_channel_remap_config(p)
                    chans = cfg.get("channels") or {}
                    if chans:
                        params = {str(n): dict(pr) for n, pr in chans.items()}
                        self._remap_cache = (p, mt, params)
                        return params, p
                except Exception as exc:
                    print(f"[Step1] remap config load failed {p}: {exc}")
        return {}, ""

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
                "2. Click 📂 Browse to load an existing segmentation params JSON\n"
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

        if not self._require_committed_fusion_settings(
                "Generating fused.zarr"):
            return
        if not self._params_match_committed_settings():
            QMessageBox.warning(
                self, "Segmentation params are from other settings",
                "These segmentation parameters were found by a search that ran "
                "on different fusion settings, so pairing them with this "
                "fusion would describe a picture neither of them saw.\n\n"
                "Run the search again on the saved settings.")
            print("[Step1] save refused: the params were searched on "
                  "other fusion settings")
            return

        # ── Commit the display mapping, once, before anything is produced ──
        # Up to here the Min/Max/Gamma the user has been adjusting lived only in
        # memory. This freezes that draft, writes it, and republishes the
        # manifest whose hash names it. If it fails, nothing is fused and no
        # existing result is touched: a fused zarr that cannot say which mapping
        # made it is exactly what this phase is removing.
        committed, reason = self._commit_display_mapping_for_save()
        if committed:
            # That commit republished the manifest, so the settings the user
            # saved now name a handoff as it was a moment ago. Follow it, or
            # stop: fusing against a snapshot the next session would refuse is
            # how "saved" and "restorable" come apart.
            rebound, why = self._rebind_fusion_settings_to_handoff()
            if not rebound:
                QMessageBox.warning(
                    self, "Fusion settings",
                    "The saved fusion settings could not be tied to the "
                    f"republished Step0 handoff, so nothing was fused.\n\n"
                    f"Reason: {why}\n\nSave the fusion settings again.")
                print(f"[Step1] save aborted: settings not rebound ({why})")
                return
        if not committed:
            QMessageBox.warning(
                self, "Save",
                "The channel display mapping could not be saved, so nothing was "
                f"fused.\n\nReason: {reason}")
            print(f"[Step1] save aborted: display mapping not committed ({reason})")
            return

        # ── Write fusion_config.json ──────────────────────────────────
        # The COMMITTED settings, the same ones every search ran on, so the
        # file and the results that led to it describe one configuration. An
        # unticked channel is not in them, whatever weight it still holds.
        snapshot = self._committed_fusion_settings() or {}
        fcfg = copy.deepcopy(snapshot.get("fusion_config")
                             or self._effective_fusion_config())
        # The mapping comes from the SAME snapshot, not from a file read back
        # through the handoff. Reading it back meant the manifest path this
        # window still held could name the mapping from before the commit that
        # had just happened: the searches would have run on the new numbers and
        # the fused.zarr been written with the old ones.
        remap_params = copy.deepcopy(snapshot.get("display_mapping") or {})
        remap_src = "the committed fusion settings"
        if not remap_params:
            remap_params, remap_src = self._load_step0_remap_params()
        if remap_params:
            print(f"[Step1] fusion applies manual remap from {remap_src} "
                  f"({len(remap_params)} channels)")
        fcfg.update({
            "ome_tiff":   OME_TIFF_FILE,
            "output_dir": OUTPUT_DIR,
            "norm_low":   NORM_LOW,
            "norm_high":  NORM_HIGH,
            "channel_remap_params": remap_params,
            "saved_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        fp1 = os.path.join(OUTPUT_DIR, "fusion_config.json")
        with open(fp1, "w", encoding="utf-8") as f:
            json.dump(fcfg, f, indent=2, ensure_ascii=False)

        # ── Write timestamped segmentation params ─────────────────────
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
        if selected_method in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2, CELLPOSE_NUCLEI_CSD):
            step1_weights = {}
            for gdata in (fcfg.get("groups") or {}).values():
                for ch, weight in (gdata.get("channels") or {}).items():
                    step1_weights[str(ch)] = float(weight)
            nuc_cfg = fcfg.get("nucleus") or {}
            if nuc_cfg.get("channel"):
                step1_weights[str(nuc_cfg.get("channel"))] = float(nuc_cfg.get("weight", 0.0) or 0.0)
            hq_source = (
                self._corrected_zarr_path
                or (self.step0_output or {}).get("corrected_zarr_path")
                or ""
            )
            raw_source = getattr(self.loader, "filepath", OME_TIFF_FILE)
            roi_id = (self.step0_output or {}).get("roi_id", "")
            roi_name = (
                (self._active_roi or {}).get("name")
                or (self._active_roi or {}).get("display_name")
                or (self.step0_output or {}).get("display_name")
                or ""
            )
            available_at_save = []
            if hq_source and os.path.exists(hq_source):
                try:
                    root = zarr.open(hq_source, mode="r")
                    group = root
                    if str(root.attrs.get("mode", "")).strip().lower() == "roi_only":
                        for group_name in list(getattr(root, "group_keys", lambda: [])()):
                            candidate = root[group_name]
                            if roi_id and str(candidate.attrs.get("roi_id") or "") == str(roi_id):
                                group = candidate
                                break
                            if roi_name and str(candidate.attrs.get("roi_name") or group_name) == str(roi_name):
                                group = candidate
                                break
                    available_at_save = list(group.array_keys()) if hasattr(group, "array_keys") else list(group.keys())
                except Exception:
                    print(f"[Step1] failed to inspect HQ source zarr:\n{traceback.format_exc()}")
            hq_meta = {
                "hq_source_zarr": os.path.abspath(hq_source) if hq_source else "",
                "multichannel_source_path": os.path.abspath(hq_source) if hq_source else "",
                "raw_channel_source_path": os.path.abspath(raw_source) if raw_source else "",
                "raw_ome_path": os.path.abspath(raw_source) if raw_source else "",
                "roi_id": roi_id,
                "roi_name": roi_name,
                "roi_display_name": roi_name,
                "hq_available_channels_at_save": available_at_save,
                "hq_channels": parse_hq_channels(cpcfg.get("hq_channels") or []),
                "hq_input_mode": cpcfg.get("hq_input_mode", "selected_channels_from_source"),
                "step1_fusion_weights": step1_weights,
            }
            cpcfg.update(hq_meta)
            cpcfg.setdefault("params", {}).update(hq_meta)
            print(f"[Step1] HQ source zarr={hq_meta['hq_source_zarr']}")
            print(f"[Step1] HQ roi_id={roi_id} roi_name={roi_name}")
            print(f"[Step1] HQ selected channels={hq_meta['hq_channels']}")
            print(f"[Step1] HQ available at save={available_at_save}")
        fp_method, _ = save_segmentation_params(OUTPUT_DIR, cpcfg)
        print(f"[Save] {fp1}")
        print(f"[Save] {fp_method}")
        print(f"[Step1] saved active segmentation params={fp_method}")
        self._pending_segmentation_param_path = fp_method

        # ── Tile selection dialog ─────────────────────────────────────
        # Count active channels for RAM estimate
        is_wholecell = selected_method == CELLPOSE_WHOLECELL_FUSION
        worker_fcfg = dict(fcfg)
        if not is_wholecell:
            self._pending_fused_zarr_meta = None
            self._reused_fused_zarr_meta = False
            nuc_ch = fcfg.get("nucleus", {}).get("channel") or self.config.nucleus_channel()
            worker_fcfg = dict(fcfg)
            # DAPI-only methods use channel 1 as segmentation input, but the
            # saved zarr remains a full fusion preview/QC source: ch0 marker
            # fusion, ch1 DAPI/nucleus.
            worker_fcfg["nucleus"] = {"channel": nuc_ch, "weight": 1.0}
            expected_dapi_meta = self._expected_dapi_input_meta(worker_fcfg, selected_method)
            self._pending_dapi_input_meta = expected_dapi_meta
            self._reused_dapi_input_meta = False
            if self._try_reuse_dapi_input_zarr(expected_dapi_meta):
                return
        else:
            self._pending_dapi_input_meta = None
            self._reused_dapi_input_meta = False
            expected_fused_meta = self._expected_fused_zarr_meta(worker_fcfg, selected_method)
            self._pending_fused_zarr_meta = expected_fused_meta
            self._reused_fused_zarr_meta = False
            if self._try_reuse_fused_zarr(expected_fused_meta):
                return
        # The worker stamps these into the zarr itself, so a finished store can
        # say what it is without a sidecar vouching for it.
        identity = (self._pending_fused_zarr_meta
                    or self._pending_dapi_input_meta or {})
        worker_fcfg["artifact_kind"] = identity.get("artifact_kind") or ""
        worker_fcfg["config_hash"] = identity.get("config_hash") or ""

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
        worker = FullFusionWorker(
            loader     = self.loader,
            fusion_cfg = worker_fcfg,
            n_rows     = n_rows,
            n_cols     = n_cols,
            rois       = self._rois if self._rois else None,
        )
        self._start_fusion_worker(
            worker,
            job_name="fusion" if is_wholecell else "DAPI input zarr",
            n_rows=n_rows, n_cols=n_cols)

    def _start_fusion_worker(self, worker, job_name, n_rows, n_cols):
        """Bind a fusion job to the dataset and handoff it was started for.

        The token, not the handler, is what makes a callback legitimate: the
        business callbacks are wrapped here, because `_on_fusion_done` is also
        called synchronously by the reuse paths, where there is no worker and
        nothing to invalidate.
        """
        self._fusion_worker = worker
        self._fusion_run_id += 1
        token = {
            "run": self._fusion_run_id,
            "dataset_gen": int(self._dataset_gen_seen),
            "manifest": str((self.step0_output or {}).get("step0_manifest_path") or ""),
            "source_identity": (self.step0_output or {}).get("source_identity"),
        }
        self._fusion_token = token
        worker.progress.connect(
            self._guarded_fusion_callback(token, self._on_fusion_progress, "progress"))
        worker.finished.connect(
            self._guarded_fusion_callback(token, self._on_fusion_done, "finished"))
        worker.error.connect(
            self._guarded_fusion_callback(token, self._on_fusion_error, "error"))

        self._lock_ui()
        self._fusion_bar_widget.setVisible(True)
        self._fusion_pbar.setValue(0)
        self._fusion_lbl.setText(
            f"Starting {job_name}  {n_rows}×{n_cols} = {n_rows*n_cols} tiles…"
        )
        # Modal progress popup so the user never mistakes a long fusion for a
        # freeze. Cancel maps to the worker's cooperative stop.
        self._fusion_dialog = QProgressDialog(
            f"Generating {job_name}…\nThis can take a while for large images.",
            "Cancel", 0, 100, self)
        self._fusion_dialog.setWindowTitle("Step1 — Fusion")
        self._fusion_dialog.setWindowModality(Qt.WindowModal)
        self._fusion_dialog.setMinimumDuration(0)
        self._fusion_dialog.setAutoClose(False)
        self._fusion_dialog.setAutoReset(False)
        self._fusion_dialog.canceled.connect(worker.stop)
        self._fusion_dialog.setValue(0)
        self._fusion_dialog.show()
        worker.start()
        return token

    def _fusion_callback_allowed(self, token):
        """Is this fusion job still the one allowed to write Step1 state?

        A job is allowed only while it is THE current job, started for the
        dataset generation that is still current and for the handoff Step1 is
        still bound to.  Anything else is a late answer about a slide the user
        has left.
        """
        current = self._fusion_token
        if current is None or token is not current:
            return False
        if int(token.get("dataset_gen", 0)) != int(self._dataset_gen_seen):
            return False
        expected = str(token.get("manifest") or "")
        bound = str((self.step0_output or {}).get("step0_manifest_path") or "")
        if expected:
            if not bound or os.path.abspath(expected) != os.path.abspath(bound):
                return False
        # The manifest path can be reused by a different slide written into the
        # same directory, so the raw identity is compared as well: it is what
        # the authoritative reader itself validates.
        if token.get("source_identity") != (self.step0_output or {}).get("source_identity"):
            return False
        return True

    def _guarded_fusion_callback(self, token, handler, label):
        """Wrap a fusion callback so a retired job's answer is dropped."""
        def _dispatch(*args):
            if not self._fusion_callback_allowed(token):
                print(f"[Step1] dropped late fusion {label} from run "
                      f"{token.get('run')}: it is not the current job")
                return
            handler(*args)
        return _dispatch

    def _retire_fusion_worker(self, reason):
        """Stop the running fusion job and make sure it can never write again.

        `stop()` is cooperative, so returning from here does not mean the
        thread has ended.  Two separate things happen: the token is dropped, so
        every business callback is refused from this line on, and the QThread is
        kept alive by a strong reference until it PHYSICALLY finishes.  Physical
        exit is `QThread.finished()`, which the worker's own `finished(str)`
        shadows — the base signal is fetched explicitly rather than mistaking
        the business one for thread termination.
        """
        self._fusion_token = None
        worker = self._fusion_worker
        self._fusion_worker = None
        self._close_fusion_dialog()
        if hasattr(self, "_fusion_bar_widget"):
            self._fusion_bar_widget.setVisible(False)
        if worker is None:
            return False
        print(f"[Step1] retiring fusion job: {reason}")
        try:
            worker.stop()
        except Exception as exc:
            print(f"[Step1] fusion stop() failed: {exc}")
        if worker.isRunning():
            worker.wait(2000)
        if worker.isRunning():
            self._retired_fusion_workers.append(worker)
            try:
                physically_finished = QtCore.QThread.finished.__get__(
                    worker, QtCore.QThread)
                physically_finished.connect(
                    lambda w=worker: self._drop_retired_fusion_worker(w))
            except Exception as exc:
                print(f"[Step1] could not watch fusion thread exit: {exc}")
        return True

    def _drop_retired_fusion_worker(self, worker):
        """Release a retired thread once it has physically ended."""
        if worker.isRunning():
            return
        self._retired_fusion_workers = [
            w for w in self._retired_fusion_workers if w is not worker]

    def _close_fusion_dialog(self):
        d = getattr(self, "_fusion_dialog", None)
        if d is not None:
            d.close()
            self._fusion_dialog = None



# ══════════════════════════════════════════════════════════════════════
#  Segment + Merge Worker  (block03 + block04 combined)
# ══════════════════════════════════════════════════════════════════════
