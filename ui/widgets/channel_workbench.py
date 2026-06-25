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
from PyQt5.QtCore import Qt, pyqtSignal

from ...core.channel_remap import apply_channel_remap, compute_qupath_auto_minmax
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

# Distinct swatch colors cycled across channels (display only).
_PALETTE = [
    "#4f9dde", "#e0556a", "#5fce7e", "#e0a83a", "#a06fde",
    "#3ec7c0", "#d76fb0", "#9ec24a", "#e07b3a", "#6f8ce0",
]


class ChannelWorkbench(QtWidgets.QWidget):
    """Channel-first conditioning workbench (v13.1 prototype).

    Signals
    -------
    refresh_requested() : the user asked to (re)load the host's current data
        (e.g. Step3's current ROI). The host connects this to its adapter and
        calls set_channel_images(). The workbench stays ignorant of Step3.
    """

    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # ── model ─────────────────────────────────────────────────────
        self._names = []                 # list[str], display order
        self._raw = {}                   # name -> np.ndarray (preview patch)
        self._params = {}                # name -> normalized params dict
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
        self._layer_list = ChannelLayerList()
        self._layer_list.active_changed.connect(self._on_active_changed)
        self._layer_list.visibility_changed.connect(self._on_visibility_changed)
        left_l.addWidget(self._layer_list, stretch=1)

        # (#2 declutter) "Select all markers" / "Clear all markers" buttons
        # removed to save space — channels stay individually toggleable via the
        # layer list. A single all-toggle is deferred to the #4 checkbox redesign.
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
        cl.addLayout(self._build_reference_bar())
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
        bar.addWidget(btn_demo)

        btn_file = QtWidgets.QPushButton("Load preview image…")
        btn_file.clicked.connect(self._load_from_file)
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
                               show_internal_save=None):
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
        """
        if refresh_label is not None and hasattr(self, "_btn_host_refresh"):
            self._btn_host_refresh.setText(str(refresh_label))
        if refresh_tooltip is not None and hasattr(self, "_btn_host_refresh"):
            self._btn_host_refresh.setToolTip(str(refresh_tooltip))
        if show_internal_save is not None and hasattr(self, "_btn_save_internal"):
            self._btn_save_internal.setVisible(bool(show_internal_save))

    # ── public API (host feeds data here) ─────────────────────────────
    def set_channel_images(self, channel_images, colors=None, context=None,
                           source="manual", source_policy=None,
                           channel_metadata=None):
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

        # Normalize + validate: keep only real 2D arrays.
        clean = {}
        skipped = []
        for name, img in (channel_images or {}).items():
            arr = self._coerce_2d(img)
            if arr is None:
                skipped.append(str(name))
                continue
            clean[str(name)] = arr

        if not clean:
            self.clear_channel_images()
            if skipped:
                self._status_lbl.setText(
                    "No usable 2D channel images "
                    f"(skipped: {', '.join(skipped)}).")
            return

        self._names = list(clean.keys())
        self._raw = clean
        self._params = {}
        self._colors = {}
        self._visible = {}
        for i, n in enumerate(self._names):
            params = default_channel_remap_params()
            arr = self._raw[n]
            finite = arr[np.isfinite(arr)] if arr.size else arr
            if finite.size:
                params["min"] = float(finite.min())
                params["max"] = float(max(finite.max(), finite.min() + 1.0))
            else:
                params["min"], params["max"] = 0.0, 1.0
            self._params[n] = normalize_channel_remap_params(params)
            self._colors[n] = (colors or {}).get(n, _PALETTE[i % len(_PALETTE)])
            self._visible[n] = True

        self._refresh_layer_list()
        self._set_controls_enabled(True)
        self._active = None
        # New patch -> fit the view once on the first preview below.
        self._canvas.request_fit()
        self._layer_list.set_active(self._names[0])
        self._on_active_changed(self._names[0])
        self._update_status(skipped=skipped)

    def clear_channel_images(self):
        """Drop all channel data and show the friendly empty state."""
        self._names = []
        self._raw = {}
        self._params = {}
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
        shape = self._raw[self._names[0]].shape
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

    def _on_active_changed(self, name):
        if name not in self._params:
            return
        self._active = name
        self._load_params_into_controls(name)
        self._refresh_preview()

    def _on_visibility_changed(self, name, visible):
        self._visible[name] = bool(visible)
        # visibility is display-only for the prototype; preview shows active ch.

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
            self._chk_enabled.setChecked(bool(p["enabled"]))
            self._lbl_bright.setText(f"{p['brightness']:.2f}")
            self._lbl_contrast.setText(f"{p['contrast']:.2f}")
            self._lbl_gamma.setText(f"{p['gamma']:.2f}")
            self._histogram.set_data(self._raw[name], p["min"], p["max"])
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
        p["enabled"] = self._chk_enabled.isChecked()

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
        self._load_params_into_controls(self._active)
        self._layer_list.update_mini(self._active, p["weight"], "w")
        self._refresh_preview()

    def _on_fit_view(self):
        """Explicit user-triggered fit-to-view."""
        self._canvas.request_fit()
        self._refresh_preview()

    # ── preview ───────────────────────────────────────────────────────
    def _refresh_preview(self):
        if self._active is None:
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
        for w in (self._sp_min, self._sp_max, self._sl_bright, self._sl_contrast,
                  self._sl_gamma, self._btn_auto, self._btn_reset,
                  self._chk_enabled):
            w.setEnabled(enabled)
