"""
block01/ui/widgets/high_quality_image_viewer.py — v14.3 reusable core viewer.

HighQualityImageViewer is the extracted, reusable core of the former
ChannelViewerCanvas (see docs/v14/BLOCK01_v14_project_outline_v2.md §6). It is
the single napari-quality image surface used across Step0/Step1/Step3; v14.3
extracts it unchanged so later phases (TissueNavigatorPopup viewport sync,
Background Correction, Channel Conditioning) build on one stable viewer.

Backend (preserved, NOT rewritten):
    pyqtgraph GraphicsLayoutWidget -> ViewBox -> ImageItem
No QLabel/QPixmap, no VisPy. Float32 [0,1] arrays through ImageItem; aspect
locked, high quality.

Supports:
  - the active marker channel (raw or remapped, grayscale primary),
  - a raw-vs-remapped split view,
  - optional REFERENCE layers composited over the active marker:
        * DAPI / nuclei  (additive blue tint),
        * mask           (outline overlay from existing label arrays),
        * fusion         (alpha-blended structural map).

Reference layers are visualization only — they are NOT marker remap channels
and never reach channel_remap_config["channels"].

Fixed zoom/pan behavior (preserved): display-parameter changes (Min/Max/gamma/
brightness/contrast/opacity/channel-toggle/overlay) must NEVER reset zoom/pan.
The view is fitted (autoRange) ONLY on first load, an explicit request_fit()
(new patch), or an image SHAPE change — guarded by _need_fit / _prev_shape in
_maybe_fit. (This is the audited, fixed viewer; the older Step1 fusion-preview
_save/_restore_patch_preview_view_state path is intentionally NOT used here.)

Viewport API (v14.3, image-local pixel coordinates only — no global WSI / ROI
mapping is defined here):
    request_fit()
    get_viewport_rect() -> dict | None
    set_view_region(rect)
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtWidgets

from ...utils.mask_renderer import extract_mask_boundaries

# Canonical coordinate-space tag for the viewport API. The rect is expressed in
# the ImageItem's own pixel coordinates (axisOrder="row-major": data is indexed
# [row, col]; the ViewBox x axis maps to columns/width, y axis to rows/height).
# x0/x1 are along width, y0/y1 along height. invertY only flips DISPLAY
# direction; ViewBox.viewRange() values stay in these data/pixel coordinates.
VIEWPORT_COORDINATE_SPACE = "image_local_pixels"

# Reference-layer overlay colors (RGB floats).
_DAPI_COLOR = np.array([0.25, 0.45, 1.0], dtype=np.float32)   # blue nuclei
_MASK_COLOR = np.array([1.0, 0.25, 0.25], dtype=np.float32)   # red outline


def _to_display(img):
    """Coerce any array to a contiguous float32 in [0,1] for display."""
    arr = np.asarray(img).astype(np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
    return np.clip(arr, 0.0, 1.0)


def display_normalize(img, p_low=1.0, p_high=99.5):
    """Percentile-normalize an array to [0,1] for DISPLAY only.

    Reference layers (DAPI / fusion) may be native 0–255 / 0–4095 / 0–65535 /
    float. A naive clip would saturate them. This stretches finite pixels by a
    conservative percentile window so they render well regardless of dtype.

    IMPORTANT: this is display-only. It never touches marker remap parameters
    or channel_remap_config Min/Max — those live in the native array intensity
    space and are computed by core.channel_remap, not here.
    """
    arr = np.asarray(img).astype(np.float32)
    finite = np.isfinite(arr)
    if not finite.any():
        return np.zeros(arr.shape, dtype=np.float32)
    vals = arr[finite]
    lo = float(np.percentile(vals, p_low))
    hi = float(np.percentile(vals, p_high))
    if hi <= lo:  # constant / degenerate -> nothing to stretch
        lo = float(vals.min())
        hi = float(vals.max())
    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.float32)
    out = (np.where(finite, arr, lo) - lo) / (hi - lo)
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def _to_rgb01(img, p_low=1.0, p_high=99.5):
    """Coerce an RGB/gray array to display-normalized float32 [0,1] RGB.

    Normalizes by a single global percentile window across all channels so
    color ratios are preserved (display only — see display_normalize)."""
    arr = np.asarray(img).astype(np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return None
    arr = arr[:, :, :3]
    finite = np.isfinite(arr)
    if not finite.any():
        return np.zeros(arr.shape, dtype=np.float32)
    vals = arr[finite]
    lo = float(np.percentile(vals, p_low))
    hi = float(np.percentile(vals, p_high))
    if hi <= lo:
        lo, hi = float(vals.min()), float(vals.max())
    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.float32)
    out = (arr - lo) / (hi - lo)
    return np.clip(out, 0.0, 1.0).astype(np.float32)


class HighQualityImageViewer(QtWidgets.QWidget):
    """Large image canvas: active marker + optional reference-layer overlays.

    Reusable napari-quality viewer core. pyqtgraph ImageItem backend; preserves
    zoom/pan across display-parameter changes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._split = False
        self._raw = None
        self._remapped = None

        # View-fit guard: only fit-to-view on first load / new patch / shape
        # change — never on display-parameter changes (Min/Max/gamma/opacity/
        # toggles). See _refresh / _maybe_fit.
        self._prev_shape = None
        self._need_fit = True

        # Reference layers (visualization only).
        self._dapi = None            # 2D float [0,1] (display-normalized)
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
        """Provide raw and/or remapped versions of the active marker channel.

        Display parameters changing (Min/Max/gamma/opacity) call this every
        time; it must NOT refit the view. Fit happens only on shape change or
        an explicit request_fit() (new patch / first load).
        """
        self._raw = None if raw is None else _to_display(raw)
        self._remapped = None if remapped is None else _to_display(remapped)
        self._refresh()

    def request_fit(self):
        """Ask for one fit-to-view on the next refresh (new patch / first load)."""
        self._need_fit = True

    # ── viewport API (v14.3, image-local pixel coordinates) ───────────
    def get_viewport_rect(self):
        """Return the currently visible ViewBox range in image-local pixels.

        Returns None until an image has been displayed (no meaningful pixel
        geometry yet). The rect is the canonical representation:

            {
                "coordinate_space": "image_local_pixels",
                "x0": float, "x1": float,   # along image width  (columns)
                "y0": float, "y1": float,   # along image height (rows)
                "image_shape": [height, width],
            }

        x/y are in the ImageItem's pixel coordinates (see module docstring);
        this is NOT a global WSI / ROI coordinate — that mapping is out of scope
        for v14.3.
        """
        img = self._img_item.image
        if img is None:
            return None
        h, w = int(img.shape[0]), int(img.shape[1])
        (x0, x1), (y0, y1) = self._vb.viewRange()
        return {
            "coordinate_space": VIEWPORT_COORDINATE_SPACE,
            "x0": float(x0),
            "x1": float(x1),
            "y0": float(y0),
            "y1": float(y1),
            "image_shape": [h, w],
        }

    def set_view_region(self, rect):
        """Restore a view region from the get_viewport_rect() representation.

        Accepts the canonical dict (coordinate_space must be image-local pixels
        if present). Disables autoRange so a subsequent same-shape refresh keeps
        this region (display-parameter changes will not refit). No-op on a
        missing/malformed rect.
        """
        if not rect:
            return
        space = rect.get("coordinate_space", VIEWPORT_COORDINATE_SPACE)
        if space != VIEWPORT_COORDINATE_SPACE:
            raise ValueError(
                f"set_view_region expects coordinate_space="
                f"'{VIEWPORT_COORDINATE_SPACE}', got {space!r}")
        try:
            x0, x1 = float(rect["x0"]), float(rect["x1"])
            y0, y1 = float(rect["y0"]), float(rect["y1"])
        except (KeyError, TypeError, ValueError):
            return
        self._vb.disableAutoRange()
        self._vb.setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        # An explicit region replaces any pending fit request.
        self._need_fit = False

    # ── reference layers (visualization only) ─────────────────────────
    def set_reference_layers(self, dapi=None, mask=None, fusion=None):
        """Set/replace optional reference layers. Any may be None.

        dapi   : 2D intensity array (nuclei), rendered as additive blue.
        mask   : 2D label array (0=bg); rendered as an outline overlay.
        fusion : RGB or 2D structural map; alpha-blended.

        Missing/invalid layers are stored as None and never crash the UI.
        DAPI/fusion are display-normalized (percentile) so native-dtype layers
        do not saturate — display only, never affects marker remap params.
        """
        self._dapi = None if dapi is None else display_normalize(dapi)

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
        self._prev_shape = None
        self._need_fit = True
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
            self._maybe_fit(composed.shape[:2])
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
        self._maybe_fit(shape)

    def _maybe_fit(self, shape):
        """Fit-to-view only on first load / new patch / shape change.

        Preserves the user's zoom/pan across display-parameter changes
        (Min/Max, gamma, opacity, layer toggles, histogram edits)."""
        shape = tuple(int(v) for v in shape)
        if self._need_fit or shape != self._prev_shape:
            self._vb.autoRange()
            self._need_fit = False
        self._prev_shape = shape

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
