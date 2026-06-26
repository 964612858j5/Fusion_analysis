"""
block01/ui/widgets/tissue_navigator_popup.py — v14.2a Tissue Preview / ROI Navigator.

Floating, draggable, resizable, minimizable popup for SPATIAL NAVIGATION and ROI
MANAGEMENT only (see docs/v14/BLOCK01_v14_project_outline_v2.md §5). It is NOT a
second image viewer: it must never contain channel overlay, DAPI/marker quick
preview, raw/corrected comparison, remap preview, or background-correction
preview — those belong to the Step0 main viewers.

ROI draw / edit / delete and Full-WSI / ROI mode are REUSED from the existing
OverviewPanel (ui/step0/overview_panel.py); this popup hosts an OverviewPanel
instance as its body rather than reimplementing any ROI logic. v14.2a is a SHELL:
window behavior + the viewer-compatible viewport-rect API are wired and testable;
full live viewport sync and the image-local→whole-tissue rectangle overlay are
deferred to v14.2b (storing the rect now, drawing later — no guessed transforms).

Viewport rectangle uses the EXACT v14.3 HighQualityImageViewer representation:

    {
        "coordinate_space": "image_local_pixels",
        "x0": float, "x1": float,
        "y0": float, "y1": float,
        "image_shape": [height, width],
    }
"""

from __future__ import annotations

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

from ..step0.overview_panel import OverviewPanel

# Only this coordinate space is accepted — same tag the viewer emits. A rect in
# any other space (e.g. a future global WSI space) is an explicit error here, not
# something silently stored. v14.2b owns the image-local -> whole-tissue mapping.
VIEWPORT_COORDINATE_SPACE = "image_local_pixels"


class TissueNavigatorPopup(QtWidgets.QWidget):
    """Floating Tissue Preview / ROI Navigator shell (v14.2a).

    Hosts a reused OverviewPanel for whole-tissue thumbnail + ROI draw/edit/delete
    and Full-WSI/ROI mode. Stores a viewer-compatible viewport rect (overlay
    drawing deferred to v14.2b). Spatial navigation + ROI management ONLY — no
    channel/marker/remap preview.
    """

    def __init__(self, loader=None, nuc_ch="", parent=None):
        # Top-level window with a NATIVE title bar that includes minimize /
        # maximize / close. (Qt.Tool suppresses min/max on X11 — that was the
        # #1a defect; Qt.Window + the explicit button hints restores them.)
        super().__init__(
            parent,
            Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
            | Qt.WindowStaysOnTopHint,
        )
        self.setWindowTitle("Tissue Preview / ROI Navigator")
        self.setMinimumWidth(280)
        self.resize(360, 420)

        self._minimized = False
        self._restore_size = self.size()
        self._viewport_rect = None   # stored canonical image-local rect or None
        self._overview_loaded_for = None   # loader the thumbnail was loaded for

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(3)

        # ── header status bar (status text only) ──────────────────────────
        # The toolbar minimize button was removed (#1b): the window's native
        # title bar already provides minimize (restored in #1a). The collapse-
        # to-bar API (minimize_to_bar/restore_from_bar/toggle_minimized) remains
        # callable; it just no longer has a duplicate toolbar button.
        self._bar = QtWidgets.QWidget()
        bar_lay = QtWidgets.QHBoxLayout(self._bar)
        bar_lay.setContentsMargins(4, 2, 4, 2)
        bar_lay.setSpacing(6)
        self._bar_label = QtWidgets.QLabel("Tissue Preview")
        self._bar_label.setStyleSheet("color:#cdd;font-size:10px;")
        bar_lay.addWidget(self._bar_label, stretch=1)
        outer.addWidget(self._bar)

        # Slot for the host's analysis-region selector (ROI vs Full WSI). The host
        # builds + owns the selector and its handler; the popup just hosts it at
        # the top so it's the first thing the user sees (see set_region_selector).
        self._region_selector = None
        self._outer = outer

        # ── body: reused OverviewPanel (ROI draw/edit/delete, Full-WSI/ROI) ──
        # lazy=True so an empty/missing-context popup never triggers a load.
        self._overview = OverviewPanel(loader, nuc_ch, lazy=True)
        outer.addWidget(self._overview, stretch=1)

        # ── empty / missing-context hint ──────────────────────────────────
        self._empty_hint = QtWidgets.QLabel(
            "No image loaded — load data in Step0 to populate the tissue preview.")
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setStyleSheet("color:#888;font-size:10px;")
        outer.addWidget(self._empty_hint)

        self._refresh_bar_text()
        self._update_empty_state()

    def set_region_selector(self, widget):
        """Host the analysis-region selector (ROI vs Full WSI) at the top.

        The host (Step0) owns the widget and all its signal connections; this
        only reparents it into the popup (just under the header bar, above the
        overview) so it is visible and is the first control the user sees. Qt
        reparents the widget on insertion; its signals are preserved.
        """
        if widget is None:
            return
        self._region_selector = widget
        # index 1 = right after the header bar (index 0), before the overview.
        self._outer.insertWidget(1, widget)
        widget.setVisible(True)

    # ── reused overview / ROI adapter (no forked ROI logic) ──────────────
    @property
    def overview(self):
        """The reused OverviewPanel instance (single ROI model/API source)."""
        return self._overview

    def set_overview_context(self, loader=None, nuc_ch=None, rois=None,
                             patches=None, full_wsi_mode=False):
        """Thin adapter to feed the reused OverviewPanel. v14.2a stores context;
        live sync with the Step0 main viewer is v14.2b. Never creates files."""
        if loader is not None:
            self._overview.loader = loader
            try:
                self._overview.full_h = loader.shape[0]
                self._overview.full_w = loader.shape[1]
            except Exception:
                pass
        if nuc_ch is not None:
            self._overview.nuc_ch = nuc_ch
        if rois is not None or patches is not None:
            self._overview.set_rois_and_patches(
                list(rois or []), list(patches or []), bool(full_wsi_mode))
        # #7 fix: the popup's OverviewPanel is created lazy=True, so its tissue
        # thumbnail never loads on its own. Trigger the SAME load path the Step0
        # overview uses (OverviewPanel._load_overview), once per loader — not on
        # every ROI reconcile — so "Loading..." clears to the real thumbnail.
        self._ensure_overview_thumbnail_loaded()
        self._refresh_bar_text()
        self._update_empty_state()

    def _ensure_overview_thumbnail_loaded(self):
        """Load the tissue thumbnail once for the current loader (reuses the Step0
        OverviewPanel load path; no forked loader). No-op without a usable loader."""
        loader = getattr(self._overview, "loader", None)
        if loader is None or getattr(self._overview, "full_h", 0) == 0:
            return
        if loader is self._overview_loaded_for:
            return                      # already loaded for this loader
        self._overview_loaded_for = loader
        self._overview._load_overview()

    def roi_count(self):
        try:
            return len(self._overview.get_rois())
        except Exception:
            return 0

    def is_full_wsi_mode(self):
        return bool(getattr(self._overview, "full_wsi_mode", False))

    def _has_data(self):
        return getattr(self._overview, "loader", None) is not None

    def _update_empty_state(self):
        self._empty_hint.setVisible(not self._has_data())

    # ── window state: show / hide / minimize-to-bar ──────────────────────
    def minimize_to_bar(self):
        """Collapse to just the header bar (body hidden, window shrunk)."""
        if self._minimized:
            return
        self._restore_size = self.size()
        self._minimized = True
        self._overview.setVisible(False)
        self._empty_hint.setVisible(False)
        self._refresh_bar_text()
        self.setFixedHeight(self._bar.sizeHint().height() + 12)

    def restore_from_bar(self):
        """Restore the full popup from the minimized bar."""
        if not self._minimized:
            return
        self._minimized = False
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)   # Qt QWIDGETSIZE_MAX
        self._overview.setVisible(True)
        self._update_empty_state()
        self.resize(self._restore_size)
        self._refresh_bar_text()

    def toggle_minimized(self):
        if self._minimized:
            self.restore_from_bar()
        else:
            self.minimize_to_bar()

    def is_minimized(self):
        return self._minimized

    def _refresh_bar_text(self):
        mode = "Full WSI" if self.is_full_wsi_mode() else "ROI mode"
        view = "Current View" if self._viewport_rect is not None else "—"
        self._bar_label.setText(
            f"Tissue Preview | {mode} | ROI: {self.roi_count()} | {view}")

    # ── viewport rectangle API (viewer-compatible; image-local pixels) ───
    def set_viewport_rect(self, rect):
        """Store the main viewer's current viewport rect (v14.3 representation).

        Accepts ONLY coordinate_space == "image_local_pixels". A mismatched space
        is an explicit failure (ValueError) — never silently stored or dropped.
        v14.2a stores the rect; drawing it on the whole-tissue overview needs the
        image-local -> tissue mapping and is deferred to v14.2b.
        """
        if not isinstance(rect, dict):
            raise ValueError("viewport rect must be a dict")
        space = rect.get("coordinate_space")
        if space != VIEWPORT_COORDINATE_SPACE:
            raise ValueError(
                f"set_viewport_rect expects coordinate_space="
                f"'{VIEWPORT_COORDINATE_SPACE}', got {space!r}")
        stored = {
            "coordinate_space": VIEWPORT_COORDINATE_SPACE,
            "x0": float(rect["x0"]),
            "x1": float(rect["x1"]),
            "y0": float(rect["y0"]),
            "y1": float(rect["y1"]),
        }
        shp = rect.get("image_shape")
        if shp is not None:
            stored["image_shape"] = [int(shp[0]), int(shp[1])]
        self._viewport_rect = stored
        self._refresh_bar_text()
        return True

    def clear_viewport_rect(self):
        self._viewport_rect = None
        self._refresh_bar_text()

    def current_viewport_rect(self):
        """Return the stored image-local viewport rect, or None."""
        return None if self._viewport_rect is None else dict(self._viewport_rect)
