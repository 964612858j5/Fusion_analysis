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


# ── Regions (F4-2, docs/f4_regions_downstream_plan.md v2.1) ────────────
#: Which kinds read the fused (membrane) channel besides the nucleus.
_READS_FUSION = {"fusion_fusion_nucleus", "nucleus_fusion"}


def seg_channels(method, fusion_config):
    """The channel names a method's input is made of (its segmentation
    channels S): the nucleus channel, plus every channel fused with a weight
    for the kinds that read the fused channel. An exclusion touching one of
    these invalidates segmentation; others only invalidate measurements."""
    fcfg = fusion_config or {}
    nucleus = fcfg.get("nucleus") or {}
    out = set()
    if nucleus.get("channel") and float(nucleus.get("weight", 0.0) or 0.0) > 0:
        out.add(str(nucleus["channel"]))
    if INPUT_KIND.get(method) in _READS_FUSION:
        for data in (fcfg.get("groups") or {}).values():
            if float(data.get("group_weight", 1.0) or 0.0) <= 0:
                continue
            out |= {str(ch) for ch, w in (data.get("channels") or {}).items() if float(w or 0.0) > 0}
    return sorted(out)


def _keep_only(arr, keep_ids):
    lut_max = int(arr.max()) if arr.size else 0
    if lut_max == 0:
        return arr
    lut = np.zeros(lut_max + 1, dtype=arr.dtype)
    ids = np.fromiter((i for i in keep_ids if 0 < i <= lut_max), dtype=np.int64)
    lut[ids] = ids.astype(arr.dtype)
    return lut[arr]


def regions_filter(method, raw, window, raster):
    """The window's results with only the objects the Regions rules keep
    (`core.regions.objects`): judged on the whole predicted objects of the
    read window, BEFORE the patch crop. A nucleus follows the cell it lies in
    (expansion methods: the same id; two outputs such as Mesmer
    nuclear-guided: the cell holding most of its pixels); a nucleus in no
    cell, or a nuclei-only output, is judged on its own. Returns (raw,
    {output: dropped count})."""
    from .regions.objects import ObjectTally
    core, seg = raster.core_index(window), raster.seg_invalid(window)

    def judge(arr):
        t = ObjectTally(tma=raster.tma)
        t.add(arr, core, seg)
        v = t.verdicts()
        return {i for i, x in v.items() if x.keep}, len(v)

    out, dropped = dict(raw), {}
    cell, nuc = raw.get("cell"), raw.get("nucleus")
    if cell is not None:
        keep, n = judge(cell)
        dropped["cell"] = n - len(keep)
        out["cell"] = _keep_only(cell, keep)
        if nuc is not None and method in _expansion_methods():
            out["nucleus"] = _keep_only(nuc, keep)
            return out, dropped
    if nuc is not None:
        own_keep, n = judge(nuc)
        if cell is not None:                      # follow the parent cell where there is one
            fg = nuc > 0
            pairs = np.unique(np.stack([nuc[fg], cell[fg]]).astype(np.int64), axis=1, return_counts=True)
            best = {}
            for (ni, ci), c in zip(pairs[0].T.tolist(), pairs[1].tolist()):
                if ci and c > best.get(ni, (0, 0))[1]:
                    best[ni] = (ci, c)
            own_keep = {i for i in set(np.unique(nuc[fg]).tolist())
                        if (best[i][0] in keep if i in best else i in own_keep)}
        dropped["nucleus"] = n - len(own_keep)
        out["nucleus"] = _keep_only(nuc, own_keep)
    return out, dropped


def _expansion_methods():
    from ..seg_runner import engines as seg_engines
    return seg_engines.EXPANSION_METHODS
