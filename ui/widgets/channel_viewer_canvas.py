"""
block01/ui/widgets/channel_viewer_canvas.py — v13.1 center viewer canvas.

Center column of the channel-first viewer (see
docs/v13_1_channel_conditioning/04_UI_REDESIGN_SPEC.md).

A large PyQtGraph image canvas. Supports:
  - the active marker channel (raw or remapped, grayscale primary),
  - a raw-vs-remapped split view,
  - optional REFERENCE layers composited over the active marker:
        * DAPI / nuclei  (additive blue tint),
        * mask           (outline overlay from existing label arrays),
        * fusion         (alpha-blended structural map).

Reference layers are visualization only — they are NOT marker remap channels
and never reach channel_remap_config["channels"]. The active marker stays the
primary object being adjusted; reference layers sit above it at < 1 opacity so
the marker signal remains visible.

No raw/corrected source text is shown (spec). No napari. Host-agnostic: the
workbench feeds ready-to-display arrays.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtWidgets

from ...utils.mask_renderer import extract_mask_boundaries

# Reference-layer overlay colors (RGB floats).
_DAPI_COLOR = np.array([0.25, 0.45, 1.0], dtype=np.float32)   # blue nuclei
_MASK_COLOR = np.array([1.0, 0.25, 0.25], dtype=np.float32)   # red outline


def _to_display(img):
    """Coerce any array to a contiguous float32 in [0,1] for display."""
    arr = np.asarray(img).astype(np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
    return np.clip(arr, 0.0, 1.0)


def _to_rgb01(img):
    """Coerce an RGB/gray array to float32 [0,1] RGB (H,W,3), or None."""
    arr = np.asarray(img).astype(np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return None
    arr = arr[:, :, :3]
    hi = float(arr.max()) if arr.size else 0.0
    if hi > 1.0:  # e.g. uint8 RGB
        arr = arr / 255.0
    return np.clip(arr, 0.0, 1.0)


class ChannelViewerCanvas(QtWidgets.QWidget):
    """Large image canvas: active marker + optional reference-layer overlays."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._split = False
        self._raw = None
        self._remapped = None

        # Reference layers (visualization only).
        self._dapi = None            # 2D float [0,1]
        self._mask_outline = None    # 2D bool
        self._fusion = None          # (H,W,3) float [0,1]
        self._show = {"dapi": False, "mask": False, "fusion": False}
        self._opacity = {"dapi": 0.6, "mask": 0.8, "fusion": 0.5}

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#0a0a0a")
        self._vb = self._glw.addViewBox(row=0, col=0)
        self._vb.setAspectLocked(True)
        self._vb.invertY(True)
        self._img_item = pg.ImageItem(axisOrder="row-major")
        self._vb.addItem(self._img_item)
        lay.addWidget(self._glw, stretch=1)

        self._status = QtWidgets.QLabel("No image loaded")
        self._status.setStyleSheet("color:#888;font-size:10px;")
        lay.addWidget(self._status)

    # ── active marker ─────────────────────────────────────────────────
    def set_split(self, enabled):
        self._split = bool(enabled)
        self._refresh()

    def set_images(self, raw=None, remapped=None):
        """Provide raw and/or remapped versions of the active marker channel."""
        self._raw = None if raw is None else _to_display(raw)
        self._remapped = None if remapped is None else _to_display(remapped)
        self._refresh()

    # ── reference layers (visualization only) ─────────────────────────
    def set_reference_layers(self, dapi=None, mask=None, fusion=None):
        """Set/replace optional reference layers. Any may be None.

        dapi   : 2D intensity array (nuclei), rendered as additive blue.
        mask   : 2D label array (0=bg); rendered as an outline overlay.
        fusion : RGB or 2D structural map; alpha-blended.

        Missing/invalid layers are stored as None and never crash the UI.
        """
        self._dapi = None if dapi is None else _to_display(dapi)

        if mask is None:
            self._mask_outline = None
        else:
            try:
                outline = extract_mask_boundaries(np.asarray(mask))
                self._mask_outline = np.asarray(outline, dtype=bool)
            except Exception:
                self._mask_outline = None

        self._fusion = None if fusion is None else _to_rgb01(fusion)
        self._refresh()

    def available_reference_layers(self):
        """Return {name: bool} of which reference layers are present."""
        return {
            "dapi": self._dapi is not None,
            "mask": self._mask_outline is not None,
            "fusion": self._fusion is not None,
        }

    def set_layer_visibility(self, **flags):
        for k, v in flags.items():
            if k in self._show:
                self._show[k] = bool(v)
        self._refresh()

    def set_layer_opacity(self, **vals):
        for k, v in vals.items():
            if k in self._opacity:
                self._opacity[k] = float(np.clip(v, 0.0, 1.0))
        self._refresh()

    def clear(self):
        self._raw = None
        self._remapped = None
        self._img_item.clear()
        self._status.setText("No image loaded")

    def clear_reference_layers(self):
        self._dapi = None
        self._mask_outline = None
        self._fusion = None
        self._refresh()

    # ── rendering ─────────────────────────────────────────────────────
    def _refresh(self):
        primary = self._remapped if self._remapped is not None else self._raw
        if primary is None:
            self.clear()
            return

        # Split view is grayscale-only (raw | remapped); overlays not applied.
        if self._split and self._raw is not None and self._remapped is not None:
            composed = self._compose_split(self._raw, self._remapped)
            self._img_item.setImage(composed, levels=(0.0, 1.0))
            self._status.setText("Split view — left: raw   right: remapped")
            self._vb.autoRange()
            return

        rgb = np.stack([primary] * 3, axis=-1).astype(np.float32)
        active_overlays = []
        shape = primary.shape[:2]

        # fusion (alpha blend, below the tints)
        if self._show["fusion"] and self._fusion is not None \
                and self._fusion.shape[:2] == shape:
            a = self._opacity["fusion"]
            rgb = rgb * (1.0 - a) + self._fusion * a
            active_overlays.append("fusion")

        # DAPI (additive blue nuclei)
        if self._show["dapi"] and self._dapi is not None \
                and self._dapi.shape[:2] == shape:
            a = self._opacity["dapi"]
            rgb = rgb + (self._dapi[:, :, None] * _DAPI_COLOR[None, None, :]) * a
            active_overlays.append("DAPI")

        # mask outline (on top)
        if self._show["mask"] and self._mask_outline is not None \
                and self._mask_outline.shape[:2] == shape:
            a = self._opacity["mask"]
            ol = self._mask_outline
            rgb[ol] = rgb[ol] * (1.0 - a) + _MASK_COLOR[None, :] * a
            active_overlays.append("mask")

        rgb = np.clip(rgb, 0.0, 1.0)
        self._img_item.setImage(rgb, levels=(0.0, 1.0))

        label = "remapped" if self._remapped is not None else "raw"
        extra = (" + " + " + ".join(active_overlays)) if active_overlays else ""
        self._status.setText(f"Active channel — {label}{extra}")
        self._vb.autoRange()

    @staticmethod
    def _compose_split(raw, remapped):
        """Left half = raw, right half = remapped, with a thin divider."""
        if raw.shape != remapped.shape:
            return remapped
        h, w = raw.shape[:2]
        mid = w // 2
        out = remapped.copy()
        out[:, :mid] = raw[:, :mid]
        if 0 <= mid < w:
            out[:, mid:mid + 1] = 1.0
        return out
