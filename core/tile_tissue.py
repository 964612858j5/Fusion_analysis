"""Which Step2 tiles hold tissue (block S2T; docs/v16_Step2_tiles_application.md).

One tissue mask per slide, from the raw slide's nucleus channel at the
pyramid level closest to 16x and not finer (the same level and the same
`random_patches.tissue_mask` Step1's random patches use), read through the
`PixelSource` contract. A tile's tissue fraction is measured on its READ
window (own region + halo), mapped onto that level with the exact per-axis
ratios (`project_identity.SlideFrames`) and covering it -- a level pixel that
touches the window counts. A tile is proposed for skipping only when its
fraction is exactly 0: not a single tissue pixel in the window.

Pure numpy / scipy plus the contract; no Qt. The Step2 page builds the plan
and the user confirms it; the worker only executes the final list.
"""

import json
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import random_patches as rp
from .project_identity import SlideFrames


class TissueUnavailable(ValueError):
    """The fused input does not say where it lies on which slide."""


def fused_region(fused_zarr_path) -> Tuple[Tuple[int, int, int, int], str, Optional[str]]:
    """``(bbox_fullres, slide path, nucleus channel name)`` of a Step1 fused
    product: its bbox from its own attrs, the slide from its workspace's
    manifest, the nucleus channel from the workspace's Step1 settings (else
    the Step0 handoff). Raises `TissueUnavailable` rather than guess."""
    import zarr
    path = os.path.abspath(fused_zarr_path)
    z = zarr.open(path, mode="r")
    bbox = z.attrs.get("bbox_fullres")
    if not bbox or len(bbox) != 4:
        raise TissueUnavailable("the fused input records no bbox_fullres")
    bbox = tuple(int(v) for v in bbox)
    if (bbox[1] - bbox[0], bbox[3] - bbox[2]) != tuple(int(v) for v in z.shape[:2]):
        raise TissueUnavailable(f"the fused input is {tuple(z.shape[:2])}, its bbox {list(bbox)}")
    ws = os.path.dirname(os.path.dirname(path))              # <ws>/step1/<fused>.zarr
    manifest = _load(os.path.join(ws, "roi_manifest.json"))
    slide = (manifest or {}).get("source_ome")
    if not slide or not os.path.isfile(slide):
        raise TissueUnavailable("the workspace records no readable slide")
    settings = _load(os.path.join(ws, "step1", "step1_fusion_settings.json")) or {}
    channel = ((settings.get("fusion_config") or {}).get("nucleus") or {}).get("channel")
    if not channel:
        handoff = _load(os.path.join(ws, "step0", "step0_roi_result.json")) or {}
        channel = handoff.get("nucleus_channel") or handoff.get("panel_nucleus")
    if not channel:
        raise TissueUnavailable("no nucleus channel is recorded for this workspace")
    return bbox, slide, str(channel)


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class SlideTissue:
    """The tissue mask of one slide channel at its ~16x level."""

    def __init__(self, slide_path, channel):
        from ..sources.ome_tiff import OmeTiffSource
        with OmeTiffSource(slide_path) as src:
            shapes = [src.level_shape(lv) for lv in range(src.level_count())]
            frames = SlideFrames(shapes)
            downsamples = [max(frames.scale_yx(lv)) for lv in range(len(shapes))]
            self.level = rp.pick_mask_level(downsamples)
            h, w = shapes[self.level]
            signal, _ = src.read_region(channel, self.level, 0, h, 0, w)
        self.slide = os.path.abspath(slide_path)
        self.channel = str(channel)
        self.scale_yx = frames.scale_yx(self.level)
        self.mask = rp.tissue_mask(signal, max(self.scale_yx))
        self.params = dict(rp.PARAMS_L0)

    def fraction(self, read_bbox_global: Sequence[int]) -> float:
        """Tissue share of a global ``[y0, y1, x0, x1)`` window, on the level
        pixels that touch it."""
        y0, y1, x0, x1 = (int(v) for v in read_bbox_global)
        sy, sx = self.scale_yx
        h, w = self.mask.shape
        ly0, ly1 = max(0, math.floor(y0 / sy)), min(h, math.ceil(y1 / sy))
        lx0, lx1 = max(0, math.floor(x0 / sx)), min(w, math.ceil(x1 / sx))
        if ly1 <= ly0 or lx1 <= lx0:
            return 0.0
        return float(self.mask[ly0:ly1, lx0:lx1].mean())

    def describe(self) -> Dict:
        return {"slide": self.slide, "channel": self.channel, "level": int(self.level),
                "scale_yx": [float(v) for v in self.scale_yx], "params": self.params,
                "rule": "skip only a tile whose read window (own + halo) holds no tissue pixel"}


def tile_plan(tissue: SlideTissue, fused_shape: Tuple[int, int], origin_yx: Tuple[int, int],
              n_rows: int, n_cols: int, overlap: int) -> Dict:
    """The proposal for one grid: every tile's tissue fraction and the tiles
    without tissue. Tile geometry is Step2's own (`TileScheduler`)."""
    from ..utils.tile_scheduler import TileScheduler
    oy, ox = (int(v) for v in origin_yx)
    tiles = TileScheduler(int(fused_shape[0]), int(fused_shape[1]), int(n_rows), int(n_cols),
                          int(overlap)).tiles
    fractions: List[float] = []
    for t in tiles:
        ry0, ry1, rx0, rx1 = t.read_bbox
        fractions.append(tissue.fraction((ry0 + oy, ry1 + oy, rx0 + ox, rx1 + ox)))
    return {"grid": [int(n_rows), int(n_cols)], "overlap": int(overlap),
            "fractions": fractions, "auto": [i for i, f in enumerate(fractions) if f == 0.0],
            "mask": tissue.describe()}


__all__ = ["SlideTissue", "TissueUnavailable", "fused_region", "tile_plan"]
