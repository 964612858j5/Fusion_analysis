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
from PyQt5.QtCore import Qt

from ...core.channel_remap import apply_channel_remap, compute_qupath_auto_minmax
from ...utils.channel_remap_config import (
    DEFAULT_AUTO_SATURATION,
    default_channel_remap_config,
    default_channel_remap_params,
    normalize_channel_remap_params,
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
    """Channel-first conditioning workbench (v13.1 prototype)."""

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

        self._build_ui()
        self._set_controls_enabled(False)

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

        split = QtWidgets.QSplitter(Qt.Horizontal)

        # Left — channel layer list
        self._layer_list = ChannelLayerList()
        self._layer_list.active_changed.connect(self._on_active_changed)
        self._layer_list.visibility_changed.connect(self._on_visibility_changed)
        split.addWidget(self._layer_list)

        # Center — viewer
        center = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        self._canvas = ChannelViewerCanvas()
        cl.addWidget(self._canvas, stretch=1)
        view_bar = QtWidgets.QHBoxLayout()
        self._chk_remapped = QtWidgets.QCheckBox("Show remapped")
        self._chk_remapped.setChecked(True)
        self._chk_remapped.toggled.connect(lambda _v: self._refresh_preview())
        self._chk_split = QtWidgets.QCheckBox("Split raw | remapped")
        self._chk_split.toggled.connect(self._canvas.set_split)
        view_bar.addWidget(self._chk_remapped)
        view_bar.addWidget(self._chk_split)
        view_bar.addStretch()
        cl.addLayout(view_bar)
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
        self._sp_min.setRange(-1e9, 1e9)
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

    def _build_bottom_bar(self):
        bar = QtWidgets.QHBoxLayout()
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
        bar.addWidget(btn_save)
        return bar

    # ── public API (host feeds data here) ─────────────────────────────
    def set_channel_images(self, images, colors=None):
        """Load a set of named channel preview patches.

        Parameters
        ----------
        images : dict[str, np.ndarray]
            channel name -> 2D preview patch (raw intensities).
        colors : dict[str, str], optional
            channel name -> hex color for the swatch.
        """
        self._names = list(images.keys())
        self._raw = {n: np.asarray(images[n]) for n in self._names}
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
        self._set_controls_enabled(bool(self._names))
        if self._names:
            self._layer_list.set_active(self._names[0])
            self._on_active_changed(self._names[0])
        self._info_lbl.setText(
            f"{len(self._names)} channels loaded "
            f"({'×'.join(str(d) for d in self._raw[self._names[0]].shape)})"
            if self._names else "No preview loaded"
        )

    def build_config(self):
        """Build (and normalize) the current segmentation_preprocess_config."""
        cfg = default_channel_remap_config()
        cfg["auto_saturation"] = self._auto_saturation
        for n in self._names:
            cfg["channels"][n] = dict(self._params[n])
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

    # ── preview ───────────────────────────────────────────────────────
    def _refresh_preview(self):
        if self._active is None:
            self._canvas.clear()
            return
        raw = self._raw[self._active]
        remapped = apply_channel_remap(raw, self._params[self._active])
        show_remapped = self._chk_remapped.isChecked()
        self._canvas.set_images(
            raw=self._normalize_raw(raw),
            remapped=remapped if show_remapped else None,
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
        self.set_channel_images(images)

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
        self.set_channel_images(images)

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
                  self._chk_enabled, self._chk_remapped, self._chk_split):
            w.setEnabled(enabled)
