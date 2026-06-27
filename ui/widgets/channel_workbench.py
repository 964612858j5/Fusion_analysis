"""
block01/ui/widgets/channel_workbench.py — v13.1 channel-first workbench.

Composes the channel-first viewer (see
docs/v13_1_channel_conditioning/04_UI_REDESIGN_SPEC.md):

    Left   : ChannelLayerList            (vertical, no horizontal scroll)
    Center : ChannelViewerCanvas         (large viewer, raw-vs-remapped split)
    Right  : active-channel inspector     (histogram + Min/Max/Brightness/
             Contrast/Gamma + Auto/Reset/Save)
    Bottom : preview load / save config

This is the Phase 2 prototype. It is intentionally self-contained and
host-agnostic: feed it channel arrays via set_channel_images(); it produces and
saves a segmentation_preprocess_config via utils.channel_remap_config. It does
NOT run segmentation, touch Step2, or write h5ad.

Phase 3 will extract the right-hand inspector into its own widget; for now it
lives here to keep the prototype in one place.
"""

from __future__ import annotations

import os

import numpy as np
from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor

from ...core.channel_remap import (
    apply_channel_remap,
    compose_multichannel_overlay,
    compute_qupath_auto_minmax,
)
from ...utils.channel_remap_config import (
    DEFAULT_AUTO_SATURATION,
    default_channel_remap_config,
    default_channel_remap_params,
    default_source_policy,
    normalize_channel_remap_params,
    normalize_source_policy,
    save_channel_remap_config,
    validate_channel_remap_config,
)
from .channel_histogram_panel import ChannelHistogramPanel
from .channel_layer_list import ChannelLayerList
from .channel_viewer_canvas import ChannelViewerCanvas

# Standard fluorescence pseudocolors cycled across channels (display only).
# DAPI is forced to blue by the host (Step0) via the colors argument.
_PALETTE = [
    "#00ff00", "#ff0000", "#00ffff", "#ff00ff", "#ffff00",
    "#ffffff", "#ff8800", "#88ff00", "#0088ff", "#ff0088",
]


def _hex_to_rgb01(color):
    """Hex / #rrggbb string -> (r, g, b) floats in [0, 1]. Robust to bad input."""
    c = QColor(color)
    if not c.isValid():
        return (1.0, 1.0, 1.0)
    return (c.redF(), c.greenF(), c.blueF())


class ChannelWorkbench(QtWidgets.QWidget):
    """Channel-first conditioning workbench (v13.1 prototype).

    Signals
    -------
    refresh_requested() : the user asked to (re)load the host's current data
        (e.g. Step3's current ROI). The host connects this to its adapter and
        calls set_channel_images(). The workbench stays ignorant of Step3.
    """

    refresh_requested = pyqtSignal()

    def __init__(self, parent=None, *, show_reference_bar=True,
                 show_enabled_checkbox=True, multichannel_overlay=False):
        super().__init__(parent)
        # Host-optional surfaces. Step0 conditioning (#6/#8) turns BOTH off: DAPI
        # is a normal conditionable channel there (no reference overlay) and
        # fusion participation is decided by Step1, not a per-channel checkbox.
        # Step1.5 / Step3 keep them on (reference layers + fusion-enable).
        self._show_reference_bar = bool(show_reference_bar)
        self._show_enabled_checkbox = bool(show_enabled_checkbox)
        # QuPath-style multi-channel overlay (Step0): checkbox = display, additive
        # pseudocolor blend of all visible channels. Off (single-channel display)
        # for Step1.5 / Step3, which keep the click-to-show one-channel viewer.
        self._multichannel_overlay = bool(multichannel_overlay)
        # ── model ─────────────────────────────────────────────────────
        self._names = []                 # list[str], display order
        self._raw = {}                   # name -> np.ndarray (preview patch)
        self._params = {}                # name -> normalized params dict
        # (#2) Min/Max/Gamma are GLOBAL per channel (QuPath-style): once the user
        # adjusts a channel, its params stick across patch switches instead of
        # being re-seeded from each new patch's pixels. Only channels never
        # adjusted get auto-seeded. Cleared on a new dataset (different names).
        self._user_adjusted = {}         # name -> bool (user changed its params)
        self._colors = {}                # name -> hex
        self._visible = {}               # name -> bool
        self._active = None              # active channel name
        self._auto_saturation = DEFAULT_AUTO_SATURATION
        self._loading = False            # guard against signal recursion
        self._source = "none"            # "none" | "step3" | "demo" | "file"
        self._context = {}               # provenance/context from the host
        self._source_policy = default_source_policy()  # intensity provenance
        self._channel_meta = {}          # name -> per-channel source metadata
        self._ref_available = {"dapi": False, "mask": False, "fusion": False}
        # Lazy-load: the host may register a provider so channels whose pixels
        # were not pre-loaded (passed as None to set_channel_images) are read
        # on-demand the first time they become active. fn(name) -> 2D array|None.
        self._pixel_provider = None
        # Progressive overlay loading: when many channels become visible at once
        # (e.g. the All toggle), the composite shows already-loaded channels
        # immediately and this timer reads the rest one-per-tick so the UI never
        # blocks on ~28 synchronous reads.
        self._progressive_timer = None

        self._build_ui()
        self._set_controls_enabled(False)
        self._update_status()

    # ── UI construction ───────────────────────────────────────────────
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        banner = QtWidgets.QLabel(
            "Channel Conditioning (v13.1 prototype) — manual remap is for "
            "segmentation only; h5ad expression stays raw/bio-corrected."
        )
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "color:#cdd;background:#202830;border:1px solid #2c3e50;"
            "border-radius:4px;padding:4px;font-size:10px;"
        )
        root.addWidget(banner)

        self._status_lbl = QtWidgets.QLabel()
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("color:#9fd; font-size:10px; padding:1px 2px;")
        root.addWidget(self._status_lbl)

        split = QtWidgets.QSplitter(Qt.Horizontal)

        # Left — channel layer list + marker bulk controls
        left = QtWidgets.QWidget()
        left_l = QtWidgets.QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)
        left_l.setSpacing(4)
        # (#3 all-toggle) A single "All" checkbox above the list — multichannel
        # overlay only (Step0). Checks/unchecks every channel at once; reflects
        # all / partial / none via a tristate. Single-channel hosts don't need it.
        if self._multichannel_overlay:
            self._chk_all = QtWidgets.QCheckBox("All")
            self._chk_all.setTristate(True)
            self._chk_all.setToolTip(
                "Show all channels / hide all channels in the overlay.")
            self._chk_all.clicked.connect(self._on_all_toggled)
            left_l.addWidget(self._chk_all)

        self._layer_list = ChannelLayerList()
        self._layer_list.active_changed.connect(self._on_active_changed)
        self._layer_list.visibility_changed.connect(self._on_visibility_changed)
        self._layer_list.color_clicked.connect(self._on_color_clicked)
        left_l.addWidget(self._layer_list, stretch=1)

        # (#2 declutter) "Select all markers" / "Clear all markers" buttons
        # were removed; the single "All" checkbox above replaces them.
        split.addWidget(left)

        # Center — viewer
        center = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        self._canvas = ChannelViewerCanvas()
        cl.addWidget(self._canvas, stretch=1)
        view_bar = QtWidgets.QHBoxLayout()
        # (#5 declutter) "Show remapped" + "Split raw|remapped" toggles removed —
        # the viewer always shows the conditioned (remapped) channel, which is the
        # whole point of this tab. is_split_view() is kept for the v14.2c viewport
        # sync; no UI path enters split now, so it always reports False.
        btn_fit = QtWidgets.QPushButton("Fit view")
        btn_fit.setToolTip("Reset zoom/pan to fit the whole patch.")
        btn_fit.clicked.connect(self._on_fit_view)
        view_bar.addWidget(btn_fit)
        view_bar.addStretch()
        cl.addLayout(view_bar)
        if self._show_reference_bar:
            cl.addLayout(self._build_reference_bar())
        else:
            # No reference overlay surface (Step0): keep the attrs so callers and
            # status code can probe them without AttributeError.
            self._ref_chk = {}
            self._ref_op = {}
        split.addWidget(center)

        # Right — active channel inspector
        split.addWidget(self._build_inspector())

        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setStretchFactor(2, 3)
        root.addWidget(split, stretch=1)

        # Bottom — preview load / save config
        root.addLayout(self._build_bottom_bar())

    def _build_inspector(self):
        box = QtWidgets.QGroupBox("Active Channel")
        box.setStyleSheet(
            "QGroupBox{border:1px solid #61afef;border-radius:5px;"
            "margin-top:6px;font-weight:bold;color:#61afef;font-size:11px;}"
        )
        lay = QtWidgets.QVBoxLayout(box)
        lay.setSpacing(6)

        self._active_lbl = QtWidgets.QLabel("(no channel)")
        self._active_lbl.setStyleSheet("color:#eee;font-size:13px;font-weight:bold;")
        lay.addWidget(self._active_lbl)

        self._histogram = ChannelHistogramPanel()
        self._histogram.window_changed.connect(self._on_histogram_window)
        lay.addWidget(self._histogram)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self._sp_min = QtWidgets.QDoubleSpinBox()
        # Intensity Min is never negative. Bound the widget at 0 structurally so
        # the user cannot enter/spin below 0 AND every setValue() path (load /
        # histogram drag / auto-minmax) is clamped to >= 0 by the spinbox itself.
        self._sp_min.setRange(0.0, 1e9)
        self._sp_min.setDecimals(1)
        self._sp_min.valueChanged.connect(self._on_minmax_changed)
        form.addRow("Min", self._sp_min)

        self._sp_max = QtWidgets.QDoubleSpinBox()
        self._sp_max.setRange(-1e9, 1e9)
        self._sp_max.setDecimals(1)
        self._sp_max.valueChanged.connect(self._on_minmax_changed)
        form.addRow("Max", self._sp_max)

        self._sl_bright, self._lbl_bright = self._slider_row(
            -100, 100, 0, form, "Brightness")
        self._sl_contrast, self._lbl_contrast = self._slider_row(
            0, 300, 100, form, "Contrast")
        self._sl_gamma, self._lbl_gamma = self._slider_row(
            10, 300, 100, form, "Gamma")
        lay.addLayout(form)

        btn_row = QtWidgets.QHBoxLayout()
        self._btn_auto = QtWidgets.QPushButton("Auto")
        self._btn_auto.setToolTip(
            "QuPath-style percentile auto contrast on the current preview patch."
        )
        self._btn_auto.clicked.connect(self._on_auto)
        self._btn_reset = QtWidgets.QPushButton("Reset")
        self._btn_reset.clicked.connect(self._on_reset)
        btn_row.addWidget(self._btn_auto)
        btn_row.addWidget(self._btn_reset)
        lay.addLayout(btn_row)

        if self._show_enabled_checkbox:
            self._chk_enabled = QtWidgets.QCheckBox("Enabled (used for fusion)")
            self._chk_enabled.setChecked(True)
            self._chk_enabled.toggled.connect(self._on_enabled_changed)
            lay.addWidget(self._chk_enabled)

        lay.addStretch()
        return box

    def _slider_row(self, lo, hi, init, form, label):
        row = QtWidgets.QHBoxLayout()
        sl = QtWidgets.QSlider(Qt.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(init)
        val_lbl = QtWidgets.QLabel(f"{init / 100.0:.2f}")
        val_lbl.setFixedWidth(40)
        val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sl.valueChanged.connect(self._on_slider_changed)
        row.addWidget(sl, stretch=1)
        row.addWidget(val_lbl)
        container = QtWidgets.QWidget()
        container.setLayout(row)
        form.addRow(label, container)
        return sl, val_lbl

    def _build_reference_bar(self):
        """Reference-layer controls (visualization only): show toggles + opacity.

        Reference layers (DAPI / mask / fusion) are NOT marker remap channels;
        they never enter channel_remap_config["channels"]. Controls are disabled
        until the host supplies the corresponding layer.
        """
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Reference:"))
        self._ref_chk = {}
        self._ref_op = {}
        for key, label in (("dapi", "DAPI"), ("mask", "Mask"), ("fusion", "Fusion")):
            chk = QtWidgets.QCheckBox(label)
            chk.setEnabled(False)
            chk.toggled.connect(
                lambda v, k=key: self._canvas.set_layer_visibility(**{k: v}))
            bar.addWidget(chk)
            self._ref_chk[key] = chk

            op = QtWidgets.QSlider(Qt.Horizontal)
            op.setRange(0, 100)
            op.setValue(int(self._canvas._opacity[key] * 100))
            op.setFixedWidth(60)
            op.setEnabled(False)
            op.valueChanged.connect(
                lambda v, k=key: self._canvas.set_layer_opacity(**{k: v / 100.0}))
            bar.addWidget(op)
            self._ref_op[key] = op
        bar.addStretch()
        return bar

    def _build_bottom_bar(self):
        bar = QtWidgets.QHBoxLayout()
        # Host-refresh button. Default label/tooltip name Step3; a non-Step3 host
        # (e.g. Step1.5) overrides them via configure_host_actions so the widget
        # text never lies about where its data comes from.
        btn_step3 = QtWidgets.QPushButton("Load current Step3 ROI")
        btn_step3.setToolTip(
            "Pull the channels currently loaded in Step3's QC viewer "
            "(current ROI/patch) into this workbench.")
        btn_step3.setStyleSheet(
            "QPushButton{background:#2c3e50;color:#cde;border-radius:3px;padding:4px;}"
            "QPushButton:hover{background:#34495e;}")
        btn_step3.clicked.connect(self.refresh_requested.emit)
        self._btn_host_refresh = btn_step3
        bar.addWidget(btn_step3)

        btn_demo = QtWidgets.QPushButton("Load demo patch")
        btn_demo.clicked.connect(self._load_demo)
        self._btn_demo = btn_demo
        bar.addWidget(btn_demo)

        btn_file = QtWidgets.QPushButton("Load preview image…")
        btn_file.clicked.connect(self._load_from_file)
        self._btn_file = btn_file
        bar.addWidget(btn_file)

        self._info_lbl = QtWidgets.QLabel("No preview loaded")
        self._info_lbl.setStyleSheet("color:#aaa;font-size:10px;")
        bar.addWidget(self._info_lbl, stretch=1)

        btn_validate = QtWidgets.QPushButton("Validate config")
        btn_validate.clicked.connect(self._on_validate)
        bar.addWidget(btn_validate)

        btn_save = QtWidgets.QPushButton("Save remap config…")
        btn_save.setStyleSheet(
            "QPushButton{background:#255;color:white;border-radius:3px;padding:4px;}"
            "QPushButton:hover{background:#377;}"
        )
        btn_save.clicked.connect(self._on_save_config)
        self._btn_save_internal = btn_save
        bar.addWidget(btn_save)
        return bar

    def configure_host_actions(self, refresh_label=None, refresh_tooltip=None,
                               show_internal_save=None, show_load_buttons=None):
        """Adapt the generic bottom-bar actions to the hosting page.

        The workbench is shared between Step3 (review/QC) and Step1.5
        (pre-segmentation conditioning). The default button text names Step3;
        a different host calls this so no label references the wrong stage and
        so a host with its own save path can hide the generic internal save.

        Parameters
        ----------
        refresh_label, refresh_tooltip : str, optional
            Override the host-refresh button text / tooltip.
        show_internal_save : bool, optional
            When False, hide the generic "Save remap config…" button (the host
            provides its own official save path).
        show_load_buttons : bool, optional
            When False, hide the manual data-load buttons (host-refresh, demo,
            file). Step0 auto-syncs its current patch (+ lazy-load) so these are
            redundant there; Step3 / Step1.5 keep them.
        """
        if refresh_label is not None and hasattr(self, "_btn_host_refresh"):
            self._btn_host_refresh.setText(str(refresh_label))
        if refresh_tooltip is not None and hasattr(self, "_btn_host_refresh"):
            self._btn_host_refresh.setToolTip(str(refresh_tooltip))
        if show_internal_save is not None and hasattr(self, "_btn_save_internal"):
            self._btn_save_internal.setVisible(bool(show_internal_save))
        if show_load_buttons is not None:
            for attr in ("_btn_host_refresh", "_btn_demo", "_btn_file"):
                btn = getattr(self, attr, None)
                if btn is not None:
                    btn.setVisible(bool(show_load_buttons))

    # ── public API (host feeds data here) ─────────────────────────────
    def set_pixel_provider(self, fn):
        """Register a lazy pixel provider. fn(name) -> 2D array (or None).

        Used by hosts that pre-load only the active channel on patch switch and
        rely on the workbench to fetch the rest on-demand when the user selects
        them. The provider must read the host's CURRENT patch/ROI.
        """
        self._pixel_provider = fn

    def active_channel(self):
        """The currently active channel name, or None."""
        return self._active

    def visible_channels(self):
        """List of channel names currently checked/visible (list order)."""
        return [n for n in self._names if self._visible.get(n)]

    def set_channel_images(self, channel_images, colors=None, context=None,
                           source="manual", source_policy=None,
                           channel_metadata=None, active=None, visible=None):
        """Load a set of named channel preview patches.

        Parameters
        ----------
        channel_images : dict[str, np.ndarray]
            channel name -> 2D preview patch (raw or display intensities). The
            host (e.g. Step3) owns ROI/patch loading; this widget only
            visualizes and edits remap parameters. Pass only the current
            ROI/patch arrays — never a full WSI.
        colors : dict[str, str], optional
            channel name -> hex swatch color.
        context : dict, optional
            free-form provenance (e.g. roi name, patch shape, source paths);
            surfaced in the status label.
        source : str
            "step3" | "demo" | "file" | "manual" — drives the status label.
        source_policy : dict, optional
            Intensity-space provenance recorded into saved configs
            (source / intensity_space / normalization / scope / preview_only).
            The arrays passed here ARE the intensity space the saved Min/Max
            live in — do not pre-normalize them differently from what this
            policy declares.
        channel_metadata : dict[str, dict], optional
            per-channel source metadata (e.g. source, intensity_space,
            normalization) merged into the saved per-channel params.

        Empty / all-invalid input is handled gracefully (no crash): the
        workbench clears and shows a friendly "no data" message.
        """
        self._source = source
        self._context = dict(context or {})
        self._source_policy = normalize_source_policy(source_policy)
        self._channel_meta = dict(channel_metadata or {})

        # Normalize + validate. A value of None marks a LAZY channel: its name
        # is kept (so the layer list + build_config still cover it) but its
        # pixels are fetched on-demand via the pixel provider when first active.
        clean = {}
        skipped = []
        for name, img in (channel_images or {}).items():
            if img is None:
                clean[str(name)] = None      # lazy placeholder
                continue
            arr = self._coerce_2d(img)
            if arr is None:
                skipped.append(str(name))
                continue
            clean[str(name)] = arr

        if not any(v is not None for v in clean.values()):
            # No real pixels at all (every channel lazy / skipped): nothing to
            # display from yet. Clear rather than show a half-built UI.
            self.clear_channel_images()
            if skipped:
                self._status_lbl.setText(
                    "No usable 2D channel images "
                    f"(skipped: {', '.join(skipped)}).")
            return

        new_names = list(clean.keys())
        # (#4) First load when there was no image before. Same-name reloads are
        # patch switches: do NOT refit (preserve zoom/pan). A genuine shape change
        # is still refitted by the viewer's _maybe_fit (shape != prev_shape).
        prior_empty = not self._names
        # (#2) A different channel set = a new dataset -> drop the sticky
        # user-adjusted flags. The SAME names (a patch switch) keep them so the
        # user's Min/Max/Gamma persist; only their per-channel params are reused.
        is_new_dataset = set(new_names) != set(self._names)
        old_params = self._params
        if is_new_dataset:
            self._user_adjusted = {}

        self._names = new_names
        self._raw = clean
        self._params = {}
        self._colors = {}
        self._visible = {}
        for i, n in enumerate(self._names):
            if (not is_new_dataset) and self._user_adjusted.get(n) and n in old_params:
                # User adjusted this channel earlier -> keep its params verbatim
                # across the patch switch (global, not re-seeded from new pixels).
                params = dict(old_params[n])
            else:
                params = default_channel_remap_params()
                arr = self._raw[n]
                finite = arr[np.isfinite(arr)] if (arr is not None and arr.size) else None
                if finite is not None and finite.size:
                    params["min"] = float(finite.min())
                    params["max"] = float(max(finite.max(), finite.min() + 1.0))
                else:
                    # Lazy / empty channel: provisional range, re-seeded from real
                    # pixels by _ensure_loaded when the channel is first activated.
                    params["min"], params["max"] = 0.0, 1.0
            self._params[n] = normalize_channel_remap_params(params)
            self._colors[n] = (colors or {}).get(n, _PALETTE[i % len(_PALETTE)])
            # Default visibility: if the host gave an explicit `visible` set, only
            # those are shown (Step0 overlay loads with just DAPI checked);
            # otherwise every channel is visible (single-channel hosts).
            self._visible[n] = (n in set(visible)) if visible is not None else True

        # Build the list with signals blocked: set_channels auto-selects row 0,
        # which would otherwise fire active_changed for the WRONG channel and (in
        # multichannel mode) auto-check it. We pick the real active explicitly.
        self._layer_list.blockSignals(True)
        self._refresh_layer_list()
        self._set_controls_enabled(True)
        self._active = None
        # (#4) Fit-to-view only on the FIRST load. Patch switches keep the user's
        # zoom/pan; a different patch SHAPE is still refitted by the viewer's
        # _maybe_fit (shape != prev_shape), so we needn't request it here.
        if prior_empty:
            self._canvas.request_fit()
        # Activate the requested channel (the one whose pixels were pre-loaded);
        # fall back to a channel that actually has pixels, else the first name.
        if active not in self._names:
            active = next((n for n in self._names if self._raw[n] is not None),
                          self._names[0])
        self._layer_list.set_active(active)
        self._layer_list.blockSignals(False)
        self._on_active_changed(active)
        self._sync_all_checkbox()
        self._update_status(skipped=skipped)

    def clear_channel_images(self):
        """Drop all channel data and show the friendly empty state."""
        self._stop_progressive_load()           # no stale loads for dropped names
        self._names = []
        self._raw = {}
        self._params = {}
        self._user_adjusted = {}
        self._colors = {}
        self._visible = {}
        self._active = None
        self._channel_meta = {}
        self._source_policy = default_source_policy()
        self._layer_list.set_channels([])
        self._canvas.clear()
        self._histogram.set_data(np.zeros((1, 1), np.float32))
        self._active_lbl.setText("(no channel)")
        self._set_controls_enabled(False)
        self._update_status()

    def has_channel_data(self):
        """True if real channel data is currently loaded."""
        return bool(self._names)

    # ── v14.2c viewport-sync accessors (public viewer API only) ──────────────
    @property
    def viewer(self):
        """The hosted HighQualityImageViewer (for viewport_changed / public API)."""
        return self._canvas

    def viewer_viewport_rect(self):
        """Current image-local viewport rect from the viewer, or None."""
        return self._canvas.get_viewport_rect()

    def is_split_view(self):
        """True when the viewer is in raw|remapped split mode (geometry mixed)."""
        return self._canvas.is_split_view()

    def set_reference_layers(self, dapi=None, mask=None, fusion=None,
                             context=None):
        """Set optional viewer reference layers (visualization only).

        Parameters
        ----------
        dapi   : 2D nuclei intensity array, or None.
        mask   : 2D label array (0=bg), or None — rendered as outline overlay.
        fusion : RGB or 2D structural map, or None.
        context : dict, optional — merged into provenance context.

        These are NOT marker remap channels: they are never written to
        channel_remap_config["channels"]. Any missing layer is fine (no crash).
        Each control is enabled only when its layer is present and is left OFF
        by default so the active marker stays primary.
        """
        if not self._show_reference_bar:
            return                      # host opted out (Step0): no reference UI
        if context:
            self._context.update(context)
        self._canvas.set_reference_layers(dapi=dapi, mask=mask, fusion=fusion)
        self._ref_available = self._canvas.available_reference_layers()
        for key, present in self._ref_available.items():
            chk = self._ref_chk[key]
            op = self._ref_op[key]
            chk.setEnabled(present)
            op.setEnabled(present)
            if not present:
                chk.blockSignals(True)
                chk.setChecked(False)
                chk.blockSignals(False)
        # B1 (Phase 5c.1): DAPI is the primary anatomical reference — show it by
        # default when available so the conditioning viewer has nuclei context.
        # Mask/Fusion stay OFF by default so the active marker stays primary.
        # DAPI remains a reference layer only: it never enters
        # channel_remap_config["channels"] and is not a marker remap channel.
        if self._ref_available.get("dapi"):
            chk = self._ref_chk["dapi"]
            chk.blockSignals(True)
            chk.setChecked(True)
            chk.blockSignals(False)
            self._canvas.set_layer_visibility(dapi=True)
        self._update_status()

    def clear_reference_layers(self):
        """Drop all reference layers and disable their controls."""
        if not self._show_reference_bar:
            return
        self._canvas.clear_reference_layers()
        self._ref_available = {"dapi": False, "mask": False, "fusion": False}
        for key in self._ref_chk:
            self._ref_chk[key].blockSignals(True)
            self._ref_chk[key].setChecked(False)
            self._ref_chk[key].blockSignals(False)
            self._ref_chk[key].setEnabled(False)
            self._ref_op[key].setEnabled(False)
        self._update_status()

    def reference_layer_availability(self):
        """Return {name: bool} of which reference layers are present."""
        return dict(self._ref_available)

    def set_context(self, context):
        """Update provenance/context without changing channel data."""
        self._context = dict(context or {})
        self._update_status()

    @staticmethod
    def _coerce_2d(img):
        """Return a 2D float32 array, or None if the input is not a 2D image."""
        if img is None:
            return None
        arr = np.asarray(img)
        if arr.ndim == 3 and arr.shape[2] == 1:
            arr = arr[:, :, 0]
        if arr.ndim != 2 or arr.size == 0:
            return None
        return arr.astype(np.float32, copy=False)

    def _update_status(self, skipped=None):
        if not self._names:
            self._status_lbl.setText(
                "No Step3 channel data available yet. Load/select an ROI in "
                "Step3 first (or use 'Load demo patch').")
            self._info_lbl.setText("No preview loaded")
            return
        _shape_src = next((self._raw[n] for n in self._names
                           if self._raw.get(n) is not None), None)
        shape = _shape_src.shape if _shape_src is not None else (0, 0)
        labels = {
            "step3": "Step3 current ROI",
            "demo": "demo data",
            "file": "manually loaded image",
            "manual": "manual",
        }
        src = labels.get(self._source, self._source)
        ctx = ""
        roi = self._context.get("roi")
        if roi:
            ctx = f"  ROI: {roi}"
        extra = f"  (skipped {len(skipped)})" if skipped else ""

        policy = self._source_policy or {}
        ispace = policy.get("intensity_space", "unknown")
        scope = policy.get("scope", "unknown")
        preview = bool(policy.get("preview_only", True))
        matches = bool(policy.get("calibration_source_matches_step2", False))
        mode = policy.get("source_alignment_mode", "unknown")
        if mode == "per_channel_native":
            intensity_note = "Markers: per-channel native mix"
        elif mode == "partial_or_preview_fallback":
            intensity_note = "Markers: preview fallback present"
        elif ispace == "unknown":
            intensity_note = "Markers: scale unknown"
        else:
            intensity_note = f"Markers: {ispace}"
        match_note = f"Step2 match: {'yes' if matches else 'no'}"
        warn = ("  ⚠ PREVIEW-ONLY (not Step2-ready)" if preview else "")
        if not matches:
            warn += " fallback"

        def _tick(name):
            return "✓" if self._ref_available.get(name) else "—"
        ref_note = ""
        if self._show_reference_bar:
            ref_note = (f"   Reference layers: DAPI {_tick('dapi')} · "
                        f"Mask {_tick('mask')} · Fusion {_tick('fusion')}")

        self._status_lbl.setText(
            f"Source: {src}{ctx}   Scope: {scope}   {intensity_note}   "
            f"{match_note}   Channels: {len(self._names)}   "
            f"Patch shape: {shape[0]} × {shape[1]}{ref_note}{extra}{warn}")
        self._status_lbl.setStyleSheet(
            "color:%s; font-size:10px; padding:1px 2px;"
            % ("#f0b020" if (preview or not matches or ispace == "unknown")
               else "#9fd"))
        self._info_lbl.setText(
            f"{len(self._names)} channels — {shape[0]}×{shape[1]}")

    def build_config(self):
        """Build (and normalize) the current segmentation_preprocess_config.

        Records intensity-space provenance (source_policy) and, per channel,
        the source metadata and the observed value range so saved Min/Max are
        never ambiguous about their intensity space.
        """
        cfg = default_channel_remap_config()
        cfg["auto_saturation"] = self._auto_saturation
        cfg["source_policy"] = normalize_source_policy(self._source_policy)
        for n in self._names:
            params = dict(self._params[n])
            meta = dict(self._channel_meta.get(n, {}))
            arr = self._raw.get(n)
            if arr is not None and arr.size:
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    meta.setdefault("value_min_observed", float(finite.min()))
                    meta.setdefault("value_max_observed", float(finite.max()))
            # Fall back to the top-level policy's intensity space if the host
            # gave no per-channel detail, so each channel is self-describing.
            meta.setdefault("intensity_space", self._source_policy.get(
                "intensity_space", "unknown"))
            meta.setdefault("source", self._source_policy.get("source", "unknown"))
            params.update(meta)
            cfg["channels"][n] = params
        return cfg

    # ── layer list / active channel ───────────────────────────────────
    def _refresh_layer_list(self):
        rows = []
        for n in self._names:
            rows.append({
                "name": n,
                "color": self._colors[n],
                "visible": self._visible[n],
                "mini_value": float(self._params[n].get("weight", 1.0)),
                "mini_label": "w",
            })
        self._layer_list.set_channels(rows)

    def _ensure_loaded(self, name):
        """Lazily fetch a channel's pixels via the provider if not yet loaded.

        On first activation of a lazy channel (pixels were None), read its
        current-patch array once and (re)seed its Min/Max from the real data.
        A single synchronous read is fast; no async needed.
        """
        if self._raw.get(name) is not None:
            return                              # cache hit for this patch
        if self._pixel_provider is None:
            return
        try:
            arr = self._coerce_2d(self._pixel_provider(name))
        except Exception as exc:                # provider failure is non-fatal
            print(f"[ChannelWorkbench] lazy load failed for {name}: {exc}")
            arr = None
        if arr is None:
            return
        self._raw[name] = arr
        if self._user_adjusted.get(name):
            return                              # (#2) keep user params; no re-seed
        params = dict(self._params.get(name, default_channel_remap_params()))
        finite = arr[np.isfinite(arr)] if arr.size else None
        if finite is not None and finite.size:
            params["min"] = float(finite.min())
            params["max"] = float(max(finite.max(), finite.min() + 1.0))
        self._params[name] = normalize_channel_remap_params(params)

    def _on_active_changed(self, name):
        if name not in self._params:
            return
        self._ensure_loaded(name)               # lazy fetch if stale/None
        self._active = name
        # QuPath interaction: selecting a row also makes it visible (checked).
        if self._multichannel_overlay and not self._visible.get(name, False):
            self._visible[name] = True
            self._layer_list.set_row_checked(name, True)
            self._sync_all_checkbox()
        self._load_params_into_controls(name)
        self._refresh_preview()

    def _on_visibility_changed(self, name, visible):
        self._visible[name] = bool(visible)
        if not self._multichannel_overlay:
            return                              # single-channel hosts: display-only
        if visible:
            # Checking a channel makes it the active (inspected) channel.
            self._ensure_loaded(name)
            self._active = name
            self._layer_list.set_active(name)
            self._load_params_into_controls(name)
        elif self._active == name:
            # Unchecking the active channel: hand active to the next still-visible
            # channel in list order, or None if nothing remains checked.
            nxt = next((n for n in self._names if self._visible.get(n)), None)
            self._active = nxt
            if nxt is not None:
                self._layer_list.set_active(nxt)
                self._load_params_into_controls(nxt)
        self._sync_all_checkbox()
        self._refresh_preview()

    def _on_all_toggled(self, checked):
        """All checkbox clicked: show or hide every channel at once, recomposite
        once. From a partial state a click checks all (Qt tristate behavior)."""
        vis = bool(checked)
        for n in self._names:
            self._visible[n] = vis
        self._layer_list.set_all_visible(vis)   # batch, no per-row signals
        if vis:
            # keep the current inspector target; pick one if there was none
            if self._active is None and self._names:
                self._active = self._names[0]
                self._ensure_loaded(self._active)
                self._load_params_into_controls(self._active)
        else:
            self._active = None                 # unchecking All clears the overlay
        self._sync_all_checkbox()
        self._refresh_preview()

    def _sync_all_checkbox(self):
        """Reflect the per-row visibility in the All checkbox: all -> Checked,
        none -> Unchecked, mixed -> PartiallyChecked. Programmatic only."""
        if not getattr(self, "_chk_all", None):
            return
        states = [bool(self._visible.get(n)) for n in self._names]
        if states and all(states):
            state = Qt.Checked
        elif any(states):
            state = Qt.PartiallyChecked
        else:
            state = Qt.Unchecked
        self._chk_all.blockSignals(True)
        self._chk_all.setCheckState(state)
        self._chk_all.blockSignals(False)

    def _on_color_clicked(self, name):
        if name not in self._colors:
            return
        chosen = QtWidgets.QColorDialog.getColor(
            QColor(self._colors[name]), self, f"Color for {name}")
        if not chosen.isValid():
            return                              # user cancelled
        hexc = chosen.name()
        self._colors[name] = hexc
        self._layer_list.set_channel_color(name, hexc)
        self._refresh_preview()

    def _load_params_into_controls(self, name):
        self._loading = True
        try:
            p = self._params[name]
            self._active_lbl.setText(name)
            self._sp_min.setValue(float(p["min"]))
            self._sp_max.setValue(float(p["max"]))
            self._sl_bright.setValue(int(round(p["brightness"] * 100)))
            self._sl_contrast.setValue(int(round(p["contrast"] * 100)))
            self._sl_gamma.setValue(int(round(p["gamma"] * 100)))
            if hasattr(self, "_chk_enabled"):
                self._chk_enabled.setChecked(bool(p["enabled"]))
            self._lbl_bright.setText(f"{p['brightness']:.2f}")
            self._lbl_contrast.setText(f"{p['contrast']:.2f}")
            self._lbl_gamma.setText(f"{p['gamma']:.2f}")
            hist_src = self._raw.get(name)
            if hist_src is None:
                hist_src = np.zeros((1, 1), np.float32)
            self._histogram.set_data(hist_src, p["min"], p["max"])
        finally:
            self._loading = False

    # ── parameter editing ─────────────────────────────────────────────
    def _collect_params_from_controls(self):
        if self._active is None:
            return
        p = self._params[self._active]
        p["min"] = float(self._sp_min.value())
        p["max"] = float(self._sp_max.value())
        p["brightness"] = self._sl_bright.value() / 100.0
        p["contrast"] = self._sl_contrast.value() / 100.0
        p["gamma"] = self._sl_gamma.value() / 100.0
        # (#2) Any control edit marks this channel user-adjusted -> its params
        # now stick across patch switches (also covers Auto, which calls here).
        self._user_adjusted[self._active] = True
        if hasattr(self, "_chk_enabled"):
            p["enabled"] = self._chk_enabled.isChecked()
        # else: no fusion-enable surface (Step0) -> keep params' default
        # enabled=True; Step1 decides fusion participation downstream.

    def _on_minmax_changed(self, _v=None):
        if self._loading or self._active is None:
            return
        self._params[self._active]["auto"] = False  # manual override
        self._collect_params_from_controls()
        self._histogram.set_window(self._sp_min.value(), self._sp_max.value())
        self._refresh_preview()

    def _on_slider_changed(self, _v=None):
        if self._loading or self._active is None:
            return
        self._lbl_bright.setText(f"{self._sl_bright.value() / 100.0:.2f}")
        self._lbl_contrast.setText(f"{self._sl_contrast.value() / 100.0:.2f}")
        self._lbl_gamma.setText(f"{self._sl_gamma.value() / 100.0:.2f}")
        self._collect_params_from_controls()
        self._refresh_preview()

    def _on_enabled_changed(self, _v=None):
        if self._loading or self._active is None:
            return
        self._collect_params_from_controls()

    def _on_histogram_window(self, lo, hi):
        if self._loading or self._active is None:
            return
        self._loading = True
        try:
            self._sp_min.setValue(lo)
            self._sp_max.setValue(hi)
        finally:
            self._loading = False
        self._params[self._active]["auto"] = False
        self._collect_params_from_controls()
        self._refresh_preview()

    def _on_auto(self):
        if self._active is None:
            return
        lo, hi = compute_qupath_auto_minmax(
            self._raw[self._active], saturation=self._auto_saturation,
            exclude_zero=False)
        lo = max(0.0, float(lo))   # Min is never negative (matches the widget bound)
        if hi <= lo:
            hi = lo + 1.0
        self._loading = True
        try:
            self._sp_min.setValue(lo)
            self._sp_max.setValue(hi)
            self._histogram.set_window(lo, hi)
        finally:
            self._loading = False
        self._params[self._active]["auto"] = True
        self._collect_params_from_controls()
        self._refresh_preview()

    def _on_reset(self):
        if self._active is None:
            return
        arr = self._raw[self._active]
        finite = arr[np.isfinite(arr)] if arr.size else arr
        p = default_channel_remap_params()
        if finite.size:
            p["min"] = float(finite.min())
            p["max"] = float(max(finite.max(), finite.min() + 1.0))
        else:
            p["min"], p["max"] = 0.0, 1.0
        self._params[self._active] = normalize_channel_remap_params(p)
        self._user_adjusted[self._active] = True   # (#2) reset is a deliberate edit
        self._load_params_into_controls(self._active)
        self._layer_list.update_mini(self._active, p["weight"], "w")
        self._refresh_preview()

    def _on_fit_view(self):
        """Explicit user-triggered fit-to-view."""
        self._canvas.request_fit()
        self._refresh_preview()

    # ── preview ───────────────────────────────────────────────────────
    def _composite_loaded_now(self):
        """Blend the VISIBLE channels that are ALREADY loaded and show them.

        Does NO IO: channels whose pixels are not yet loaded (_raw[n] is None)
        are skipped here and filled in progressively by _on_progressive_tick.
        """
        channels, colors = {}, {}
        for n in self._names:
            if not self._visible.get(n):
                continue
            arr = self._raw.get(n)
            if arr is None:
                continue                        # unloaded -> progressive timer
            channels[n] = arr
            colors[n] = _hex_to_rgb01(self._colors.get(n, "#ffffff"))
        rgb = compose_multichannel_overlay(channels, colors, self._params)
        self._canvas.set_composite(rgb)         # None -> viewer clears (black)

    def _pending_progressive_channels(self):
        """Visible channels still needing a lazy read (only if a provider exists)."""
        if self._pixel_provider is None:
            return []
        return [n for n in self._names
                if self._visible.get(n) and self._raw.get(n) is None]

    def _stop_progressive_load(self):
        if self._progressive_timer is not None:
            self._progressive_timer.stop()
            self._progressive_timer = None

    def _schedule_progressive_load(self):
        """(Re)start the one-channel-per-tick loader if visible channels remain
        unloaded. Stops any stale timer first (guards concurrent recomposites)."""
        self._stop_progressive_load()
        if not self._pending_progressive_channels():
            return
        timer = QTimer(self)
        timer.setInterval(40)                   # ~one read_region per tick
        timer.timeout.connect(self._on_progressive_tick)
        self._progressive_timer = timer
        timer.start()

    def _on_progressive_tick(self):
        # Recheck visibility every tick: a channel unchecked since scheduling is
        # skipped (stale-load guard).
        pending = self._pending_progressive_channels()
        if not pending:
            self._stop_progressive_load()
            return
        self._ensure_loaded(pending[0])         # exactly one read this tick
        self._composite_loaded_now()            # show with the newly loaded one
        if not self._pending_progressive_channels():
            self._stop_progressive_load()

    def _recomposite_overlay(self):
        """QuPath-style display: additively blend all VISIBLE channels.

        Shows already-loaded channels immediately (no blocking IO) and, if any
        visible channels are still unloaded, loads them progressively via a
        QTimer (one read per tick) so the overlay builds up without freezing.
        """
        self._composite_loaded_now()
        self._schedule_progressive_load()

    def _refresh_preview(self):
        if self._multichannel_overlay:
            self._recomposite_overlay()
            return
        if self._active is None or self._raw.get(self._active) is None:
            self._canvas.clear()
            return
        raw = self._raw[self._active]
        remapped = apply_channel_remap(raw, self._params[self._active])
        # Fixed default (#5): always show the conditioned (remapped) channel — the
        # tab's purpose. raw is still supplied as the fallback the canvas shows when
        # no remap is active, so the viewer is never blank.
        self._canvas.set_images(
            raw=self._normalize_raw(raw),
            remapped=remapped,
        )

    @staticmethod
    def _normalize_raw(raw):
        """Min-max normalize raw for display only (not a remap)."""
        arr = np.asarray(raw).astype(np.float32)
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros_like(arr)
        lo, hi = float(finite.min()), float(finite.max())
        if hi <= lo:
            return np.zeros_like(arr)
        return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)

    # ── bottom bar actions ────────────────────────────────────────────
    def _load_demo(self):
        """Synthetic multi-channel patch so the prototype runs standalone."""
        rng = np.random.default_rng(0)
        h, w = 256, 256
        yy, xx = np.mgrid[0:h, 0:w]
        images = {}
        # DAPI: blobs; CD45: speckle; PanCK: gradient + background
        dapi = np.zeros((h, w), np.float32)
        for cy, cx in rng.integers(20, 236, size=(40, 2)):
            dapi += 6000 * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / 60.0))
        images["DAPI"] = (dapi + rng.normal(200, 50, (h, w))).astype(np.float32)
        images["CD45"] = (rng.gamma(2.0, 400.0, (h, w))
                          + 300 * (xx > 180)).astype(np.float32)
        images["PanCK"] = (2000 * (xx / w) + rng.normal(500, 120, (h, w))
                           ).astype(np.float32)
        self.set_channel_images(images, source="demo")

    def _load_from_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load preview image",
            "", "Images (*.tif *.tiff *.ome.tif *.ome.tiff *.npy)")
        if not path:
            return
        try:
            images = self._read_image_channels(path)
        except Exception as exc:  # surface, do not crash the page
            QtWidgets.QMessageBox.warning(
                self, "Load failed", f"Could not load preview:\n{exc}")
            return
        if not images:
            QtWidgets.QMessageBox.information(
                self, "No channels", "No channels found in the image.")
            return
        self.set_channel_images(images, context={"path": path}, source="file")

    @staticmethod
    def _read_image_channels(path):
        """Load channels from a TIFF/NPY into a name->2D-array dict (I/O lives
        in the UI layer, never in core/channel_remap.py)."""
        if path.lower().endswith(".npy"):
            arr = np.load(path)
        else:
            import tifffile
            arr = tifffile.imread(path)
        arr = np.asarray(arr)
        if arr.ndim == 2:
            return {"ch0": arr}
        if arr.ndim == 3:
            # assume (C, H, W); pick the smallest axis as channels if ambiguous
            c_axis = int(np.argmin(arr.shape))
            arr = np.moveaxis(arr, c_axis, 0)
            return {f"ch{i}": arr[i] for i in range(arr.shape[0])}
        raise ValueError(f"unsupported image ndim={arr.ndim}")

    def _on_validate(self):
        errors = validate_channel_remap_config(self.build_config())
        if errors:
            QtWidgets.QMessageBox.warning(
                self, "Config invalid", "\n".join(errors))
        else:
            QtWidgets.QMessageBox.information(
                self, "Config valid",
                "Channel remap config is valid (used_for=segmentation_only).")

    def _on_save_config(self):
        if not self._names:
            QtWidgets.QMessageBox.information(
                self, "Nothing to save", "Load a preview and condition channels first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save channel remap config",
            os.path.join(os.getcwd(), "channel_remap_config.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            save_channel_remap_config(self.build_config(), path)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "Save failed", str(exc))
            return
        QtWidgets.QMessageBox.information(
            self, "Saved", f"Saved channel remap config:\n{path}")

    # ── helpers ───────────────────────────────────────────────────────
    def _set_controls_enabled(self, enabled):
        widgets = [self._sp_min, self._sp_max, self._sl_bright, self._sl_contrast,
                   self._sl_gamma, self._btn_auto, self._btn_reset]
        if hasattr(self, "_chk_enabled"):
            widgets.append(self._chk_enabled)
        for w in widgets:
            w.setEnabled(enabled)
