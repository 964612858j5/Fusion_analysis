"""Model inputs for the pre-segmentation run -- the application side.

The steps every method shares (plan 7.10.8 ownership table, 7.11), in order,
each done once:
  1. read window = the patch grown by the HALO on every side, cut to the
     analysis region (the ROI's bbox, or the whole slide) -- no padding;
  2. fusion with the frozen, committed windows and weights (`fuse_fullres`,
     the one Step1 fusion), quantised to uint16 as fused.zarr is;
  3. pixels outside the ROI polygon set to 0 in both channels, rasterised as
     Step2's fused.zarr writer does, over the whole bbox (`RoiMask`), so both
     steps zero the same pixels;
  4. / 65535 -> F, a change of unit, not a stretch;
  5. the array for the method (R10, plan 7.11.4) -- nothing here normalises;
     each engine runs its own standard preprocessing once.
Ownership of the result (keeping the patch centre) is `core.label_ownership`.
"""

import numpy as np

from .fusion_engine import FusionEngine

#: Which array each method is given; methods with the same kind share one.
INPUT_KIND = {
    "cellpose_wholecell_fusion": "fusion_fusion_nucleus",
    "cellpose_nuclei_dapi": "nucleus",
    "cellpose_nuclei_expansion": "nucleus",
    "stardist_nuclei_dapi": "nucleus",
    "stardist_nuclei_expansion": "nucleus",
    "mesmer_whole_cell": "nucleus_fusion",
    "mesmer_nuclear_guided": "nucleus_fusion",
    "mesmer_nuclei": "nucleus_zero",
}


class InputRefused(ValueError):
    """The frozen settings cannot give a meaningful input (said to the user)."""


def check_fusion(snapshot):
    """Refuse what would make every input meaningless (R13 / N1, 7.11.2):
    no nucleus channel, a nucleus weight of 0, or a nucleus channel with no
    committed display window (no fallback to a local percentile)."""
    fcfg = (snapshot or {}).get("fusion_config") or {}
    nucleus = fcfg.get("nucleus") or {}
    ch = str(nucleus.get("channel") or "")
    if not ch:
        raise InputRefused("no nucleus channel is set in the Fusion settings")
    if float(nucleus.get("weight", 0.0) or 0.0) <= 0:
        raise InputRefused(f"the nucleus weight of {ch} is 0, so every nucleus input would be empty")
    if not ((snapshot or {}).get("display_mapping") or {}).get(ch):
        raise InputRefused(f"{ch} has no saved display window")
    return ch


def read_window(patch_bbox, halo, bounds):
    """(y0, y1, x0, x1) = the patch grown by `halo`, cut to `bounds`."""
    y0, y1, x0, x1 = (int(v) for v in patch_bbox)
    by0, by1, bx0, bx1 = (int(v) for v in bounds)
    h = max(0, int(halo))
    win = (max(by0, y0 - h), min(by1, y1 + h), max(bx0, x0 - h), min(bx1, x1 + h))
    if win[0] >= win[1] or win[2] >= win[3]:
        raise InputRefused(f"patch {list(patch_bbox)} lies outside the analysis region")
    return win


def own_local(patch_bbox, window):
    """The patch in the window's own coordinates (half-open), clipped to it."""
    y0, y1, x0, x1 = (int(v) for v in patch_bbox)
    wy0, wy1, wx0, wx1 = window
    return (max(y0, wy0) - wy0, min(y1, wy1) - wy0, max(x0, wx0) - wx0, min(x1, wx1) - wx0)


class RoiMask:
    """The ROI polygon rasterised ONCE over the ROI's whole bbox, exactly as
    Step2's fused.zarr writer does (`FullFusionWorker._poly_mask`: vertices
    truncated relative to the bbox, `cv2.fillPoly`), kept as packed bits and
    cut per window. Rasterising each window on its own would not do: fillPoly
    clips the polygon at the canvas edge and then fills a few pixels there
    differently (measured), so the zeroed pixels would no longer be
    fused.zarr's. Building costs what Step2's writer costs: one byte per ROI
    pixel, briefly."""

    def __init__(self, polygon_fullres, roi_bbox):
        import cv2
        self.y0, y1, self.x0, x1 = (int(v) for v in roi_bbox)
        self.h, self.w = y1 - self.y0, x1 - self.x0
        canvas = np.zeros((self.h, self.w), dtype=np.uint8)
        pts = np.array([[int(x - self.x0), int(y - self.y0)] for x, y in polygon_fullres],
                       dtype=np.int32)
        cv2.fillPoly(canvas, [pts], color=1)
        self._bits = np.packbits(canvas, axis=1)
        del canvas

    def window(self, window):
        """True inside the polygon over `window` (level-0, inside the bbox)."""
        wy0, wy1, wx0, wx1 = window
        ly0, ly1, lx0, lx1 = wy0 - self.y0, wy1 - self.y0, wx0 - self.x0, wx1 - self.x0
        if ly0 < 0 or lx0 < 0 or ly1 > self.h or lx1 > self.w:
            raise ValueError(f"window {list(window)} is not inside the ROI's bbox")
        rows = np.unpackbits(self._bits[ly0:ly1], axis=1, count=self.w)
        return rows[:, lx0:lx1].astype(bool)


def fused_window(loader, window, snapshot, roi_mask=None):
    """uint16 (H, W, 2) [fusion, nucleus] over `window`, 0 outside the ROI
    polygon (`roi_mask`, a RoiMask) -- the pixels fused.zarr holds there."""
    fcfg = (snapshot or {}).get("fusion_config") or {}
    groups = {name: dict(data.get("channels") or {})
              for name, data in (fcfg.get("groups") or {}).items()}
    group_weights = {name: float(data.get("group_weight", 1.0) or 0.0)
                     for name, data in (fcfg.get("groups") or {}).items()}
    nucleus = fcfg.get("nucleus") or {}
    y0, y1, x0, x1 = window
    fused = FusionEngine().fuse_fullres(
        loader, y0, y1, x0, x1, groups, group_weights,
        str(nucleus.get("channel") or ""), float(nucleus.get("weight", 0.0) or 0.0),
        channel_remap_params=(snapshot or {}).get("display_mapping") or {})
    if roi_mask is not None:
        fused[~roi_mask.window(window)] = 0
    return fused


def model_input(kind, fused):
    """The array given to the engine for an input `kind` (plan 7.11.4)."""
    F = np.asarray(fused, dtype=np.float32) / 65535.0
    f0, f1 = F[..., 0], F[..., 1]
    if kind == "fusion_fusion_nucleus":
        return np.stack([f0, f0, f1], axis=-1)
    if kind == "nucleus":
        return np.ascontiguousarray(f1)
    if kind == "nucleus_fusion":
        return np.stack([f1, f0], axis=-1)
    if kind == "nucleus_zero":
        return np.stack([f1, np.zeros_like(f1)], axis=-1)
    raise ValueError(f"unknown input kind {kind!r}")
