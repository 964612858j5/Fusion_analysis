"""Step2's zoomable DAPI picture (docs/f5_tma_display_plan.md part 3): a
fourth `Step1WholeSlideMount` in the Tile Status tab, drawing only the
nucleus channel of the raw (or corrected) slide pyramid, with the tile grid
and the TMA cores over it. The old nucleus overview stays the fallback.

It is used only while the input Step2 shows belongs to the workspace the
main window holds (same slide, region, ROI polygon and nucleus channel) --
checked before every open, re-sync and resume -- and only on the GPU: a
viewer that cannot start closes back to the old overview and is not tried
again for the same input and slide. It never draws its rectangle on the
Tissue preview and its GPU layer has a small raw-texture budget.
"""

from __future__ import annotations

import os
from typing import Optional

from .regions.core_outlines import CoreOutlines
from .regions.overlay import RegionsOverlay

#: The small viewer's raw-texture budget (plan 3.2: 256 MB).
STEP2_RAW_TEXTURE_BYTES = 256 * 1024 * 1024


class _GridClicks:
    """The tile grid's read-only controller (plan 3.1): a click on a tile is
    `on_tile(r, c)`; everything else goes on to the view."""

    def __init__(self, tile_at, on_tile):
        self._tile_at, self._on_tile = tile_at, on_tile

    def press(self, y, x, ctrl=False):
        return False

    def click(self, y, x, ctrl=False):
        hit = self._tile_at(y, x)
        if hit is not None:
            self._on_tile(*hit)

    def wheel(self, *_a):
        return False

    def double_click(self, *_a):
        return False

    def hover(self, *_a):
        pass

    def drag(self, *_a, **_k):
        pass

    def released(self):
        pass

    def cursor_at(self, *_a):
        return None


class Step2DapiViewer:
    def __init__(self, window, page):
        self.window, self.page = window, page
        self.mount = None
        self.refused_key = None
        self.grid: Optional[RegionsOverlay] = None
        self.cores = CoreOutlines(self.view, page._core_items, page.btn_tma_cores.isChecked)

    # ── what it shows ──
    def view(self):
        m = self.mount
        stack = getattr(getattr(m, "host", None), "stack", None) if m is not None else None
        return getattr(stack, "view", None) if stack is not None else None

    def nucleus(self):
        fusion = getattr(getattr(self.window, "_display", None), "fusion", None)
        try:
            return str(fusion.nucleus()[0] or "")
        except Exception:                                   # noqa: BLE001
            return ""

    def dapi_spec(self, spec):
        """Step1's spec narrowed to the nucleus channel, in Overlay at weight
        1, with Step1's colour and window for it (plan 3.1)."""
        from .step1_draft_spec import MODE_OVERLAY, _hex_to_rgb01
        nuc = self.nucleus()
        out = {"mode": MODE_OVERLAY, "weights": {nuc: 1.0} if nuc else {}, "colors": {}, "mappings": {}}
        state = getattr(getattr(self.window, "_display", None), "state", None)
        if nuc and state is not None:
            out["colors"][nuc] = _hex_to_rgb01(state.color(nuc))
            mapping = state.mapping(nuc)
            if mapping is not None:
                out["mappings"][nuc] = tuple(float(v) for v in mapping)
        return out

    def input_key(self):
        loader = getattr(self.window, "loader", None)
        return (os.path.abspath(str(getattr(self.page, "_zarr_path", "") or "")),
                str(getattr(loader, "filepath", "") or ""))

    def compatible(self):
        """Whether the input Step2 shows is the main window's workspace: its
        slide, region box, ROI polygon and nucleus channel (plan 3.1)."""
        from ..core.tile_tissue import TissueUnavailable, fused_region
        path = getattr(self.page, "_zarr_path", None)
        loader = getattr(self.window, "loader", None)
        slide = str(getattr(loader, "filepath", "") or "")
        if not path or not slide:
            return False
        try:
            bbox, fused_slide, nuc = fused_region(path)
        except (TissueUnavailable, OSError, ValueError, KeyError):
            return False
        if os.path.realpath(fused_slide) != os.path.realpath(slide):
            return False
        roi = self.window.__dict__.get("_active_roi") or {}
        want = roi.get("bbox_fullres")
        if not want:
            try:
                h, w = loader.shape[:2]
                want = [0, int(h), 0, int(w)]
            except Exception:                               # noqa: BLE001
                return False
        if [int(v) for v in bbox] != [int(v) for v in want]:
            return False
        frozen = self.frozen_polygon(path, bbox)
        if frozen is False:
            return False                           # the input does not say: not drawn zoomable
        current = roi.get("polygon_fullres") or None
        norm = lambda p: [list(map(int, q)) for q in p] if p else None   # noqa: E731
        if norm(frozen) != norm(current):
            return False
        return bool(nuc) and str(nuc) == self.nucleus()

    @staticmethod
    def frozen_polygon(path, bbox):
        """The ROI polygon the input's own fuse run froze for its region
        (``roi_config.json`` beside it), None for a box-only region, or
        False when the input does not record one -- read from the input
        itself, whichever way (list or Browse) it was chosen."""
        import json
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(path)), "roi_config.json"),
                      encoding="utf-8") as f:
                rois = json.load(f)
        except (OSError, ValueError):
            return False
        for r in rois if isinstance(rois, list) else []:
            if [int(v) for v in (r.get("bbox_fullres") or [])] == [int(v) for v in bbox]:
                return r.get("polygon_fullres") or None
        return False

    # ── life ──
    def _build(self):
        if self.mount is None:
            from .step1_viewer_mount import Step1WholeSlideMount
            m = Step1WholeSlideMount(self.window, parent=self.window, camera_reason="step2",
                                     display_filter=self.dapi_spec, navigator=False,
                                     raw_texture_bytes=STEP2_RAW_TEXTURE_BYTES)
            m.install(self.page.overview_layout(), self.page.overview_widget())
            m.restore_legacy()                      # the old picture until this one opens
            self.mount = m
        return self.mount

    def follow(self, active):
        """Step2 on screen: open / re-sync / resume (when its input belongs
        here); elsewhere: paused. Returns whether the small viewer draws."""
        m = self.mount
        if not active:
            if m is not None:
                m.pause_requests()
            return False
        if not self.compatible():
            self.close()
            return False
        key = self.input_key()
        if self.refused_key == key:
            return False
        m = self._build()
        try:
            if m.host.stack is not None:
                m.sync_source("handoff")
                if m.paused:
                    m.resume_requests()
                else:
                    m.activate()
                if m.host.stack is None:
                    return self._failed(key, "the source could not be reopened")
            elif m.open(channel=self.nucleus()) is None:
                return self._failed(key, m.gpu_status().get("reason") or "no GPU picture")
        except Exception as exc:                            # noqa: BLE001
            return self._failed(key, exc)
        m.host.setVisible(True)
        self.page.overview_widget().setVisible(False)
        print(f"[Step2-Viewer] DAPI viewer on ({m.backend}, raw-texture budget "
              f"{m.gpu_status().get('raw_texture_budget_bytes', 0) // (1024 * 1024)} MiB)")
        self.refresh()
        return True

    def _failed(self, key, why):
        print(f"[Step2-Viewer] the zoomable DAPI picture is not used: {why}; the overview stays")
        self.refused_key = key
        self.close()
        return False

    def close(self):
        """Back to the old overview; nothing of the viewer reads any more."""
        self._detach_grid()
        self.cores.detach()
        m = self.mount
        if m is not None:
            try:
                m.close()
            except Exception as exc:                        # noqa: BLE001
                print(f"[Step2-Viewer] close failed: {exc}")
        widget = self.page.overview_widget()
        if widget is not None:
            widget.setVisible(True)
        if m is not None:
            m.host.setVisible(False)
        return True

    def reset(self):
        """The dataset moved: closed, and tried again at the next entry."""
        self.close()
        self.refused_key = None

    def shown(self):
        m = self.mount
        return m is not None and m.host.stack is not None and m.host.isVisible()

    # ── what is drawn over it ──
    def _detach_grid(self):
        if self.grid is not None:
            self.grid.detach()
            self.grid = None

    def refresh(self):
        """The tile grid (in slide pixels) and the cores over the viewer."""
        if not self.shown():
            return 0
        view = self.view()
        if self.grid is None or self.grid.graphics is not view.graphics or self.grid.vb is not view.view_box:
            self._detach_grid()
            self.grid = RegionsOverlay(view.graphics, view.view_box,
                                       _GridClicks(self.page.tile_at, self.page._on_tile_clicked))
        self.grid.set_items(self.page.tile_items())
        try:
            n = self.cores.refresh()
        except Exception as exc:                            # noqa: BLE001
            print(f"[TMA cores] step2 viewer: not drawn ({type(exc).__name__}: {exc})")
            self.cores.clear()
            n = 0
        if self.grid is not None:
            self.grid.raise_()
        return n
