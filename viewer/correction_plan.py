"""How a corrected tile at a given level is made -- ONE rule for every
corrected view in Step0 (full image, compare panels, their prefetch).

WHY (CD45RO, TMA4, 2026-10-11). The interactive path corrected a coarse
pyramid level with the radius divided by the level's downsample
(`effective_param`). That is not the saved result seen from further away:
at radius 1-2 px a cell is the size of the structuring element and the
tissue floor goes. Measured on CD45RO, tophat 15: the share of tissue
pixels with any signal is 0.93 in the saved product at every level, and
0.83 / 0.70 / 0.51 / 0.37 in the preview at downsample 2 / 4 / 8 / 16. The
per-level display gain matched the highlights only.

So three ways, chosen per level (user ruling 2026-10-11, option A):

* SAVED   -- the channel has a saved product made with exactly this method
  and parameter on this slide: the tile IS that product, block-averaged to
  the level. Exact, and no correction is run.
* NATIVE  -- downsample <= EXACT_MAX_DS (level 0 included): corrected at
  level 0 with the real parameter, then block-averaged. Exact.
* INTERACTIVE -- coarser: the old approximation, and the view says so
  (`APPROX_BADGE`).
"""

import os
import threading

import numpy as np

from ..core import saved_correction
from ..core.bg_correction import BG_CORRECTION_ALGO_VERSION
from .step1_source import (CORRECTED_DECISIONS, SOURCE_CORRECTED,
                           Step1SourceTable, reduce_corrected)
from .tile_types import QualityLevel, effective_param

#: The coarsest downsample still corrected exactly (at level 0). Level 1 of
#: a 2x pyramid; a level-1 tile costs four level-0 tiles of correction.
EXACT_MAX_DS = 2.0
_DS_SLACK = 0.05            # 46800 / 23400 is 2.0; an odd size gives 2.0001

#: The in-view note for a view that is NOT the saved result.
APPROX_BADGE = "Approximate preview — zoom in for the exact result"


def stride_of(downsample):
    return max(1, int(round(float(downsample))))


def is_exact(level, downsample):
    return int(level) <= 0 or float(downsample) <= EXACT_MAX_DS + _DS_SLACK


def plan(method, base_params, level, downsample, saved=None):
    """`(params, quality, algorithm_version)` a key at this level carries.

    `saved` is a `SavedProduct` matching (channel, method, params), or None.
    Level 0 keeps the identity it always had, so its cached tiles stay hits.
    """
    base = tuple(int(p) for p in (base_params or ()))
    if method is None:
        return base, QualityLevel.INTERACTIVE, BG_CORRECTION_ALGO_VERSION
    if saved is not None:
        return (base, QualityLevel.SAVED,
                f"{BG_CORRECTION_ALGO_VERSION}+saved:{saved.token}")
    if int(level) <= 0 or float(downsample) <= 1:
        return base, QualityLevel.INTERACTIVE, BG_CORRECTION_ALGO_VERSION
    if is_exact(level, downsample):
        return base, QualityLevel.NATIVE, BG_CORRECTION_ALGO_VERSION
    return (tuple(effective_param(p, level, downsample) for p in base),
            QualityLevel.INTERACTIVE, BG_CORRECTION_ALGO_VERSION)


def is_approximate(quality, level, downsample):
    return (quality == QualityLevel.INTERACTIVE and int(level) > 0
            and not is_exact(level, downsample))


# ── the saved product ────────────────────────────────────────────────
class SavedProduct:
    """A channel's saved corrected product, checked against a selection."""

    def __init__(self, table, channel, method, param, token):
        self.table = table
        self.channel = channel
        self.method = method
        self.param = int(param)
        self.token = str(token)

    def tile(self, rect, stride):
        """The product over LEVEL-k `rect` (y0, y1, x0, x1) at `stride`,
        block-averaged on the level-0-anchored grid. float32."""
        y0, y1, x0, x1 = (int(v) for v in rect)
        if stride > 1:
            plane = self.table.coarse_plane(self.channel, stride)
            if plane is not None:
                values, valid = plane.tile((y0, y1, x0, x1))
                return np.where(valid, values, 0.0).astype(np.float32)
        mean, valid = reduce_corrected(
            self.table.region(self.channel),
            (y0 * stride, y1 * stride, x0 * stride, x1 * stride), stride)
        out = np.where(valid, mean, 0.0).astype(np.float32)
        return out[:y1 - y0, :x1 - x0]


_CACHE_LOCK = threading.Lock()
_CACHE = {}


def saved_product(dataset_path, channel, method, base_params, slide_shape):
    """The SAVED product for this exact selection, or None.

    Matches only when the saved decision is `method`, the product's own
    attrs say it was made with the same method and parameter, it can name
    the write that made it (`source_identity`), and it covers the whole
    slide `slide_shape` (level-0 rows, cols) -- an ROI product has nothing
    to say outside its ROI, and Step0 previews the whole slide. The slide's
    size comes from the caller: the product's own `source_shape` attr is
    the shape of the corrected array, so an ROI anchored at the origin
    would vouch for itself.
    """
    if method not in CORRECTED_DECISIONS or not base_params or not slide_shape:
        return None
    slide_shape = tuple(int(v) for v in slide_shape[:2])
    path, decisions = saved_correction.saved_store(dataset_path)
    if not path or str(decisions.get(channel, "")).lower() != method:
        return None
    # every Save re-registers its loader, which moves `version()`
    key = (os.path.abspath(str(dataset_path)), saved_correction.version(),
           path, str(channel), method, int(base_params[0]), slide_shape)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    found = None
    try:
        found = _resolve(path, channel, method, int(base_params[0]),
                         slide_shape)
    except Exception:                                       # noqa: BLE001
        found = None            # a saved product is an optimisation
    with _CACHE_LOCK:
        if len(_CACHE) > 512:
            _CACHE.clear()
        _CACHE[key] = found
    return found


def _resolve(path, channel, method, param, slide_shape):
    table = Step1SourceTable(decisions={channel: method},
                             corrected_zarr_path=path)
    if table.source_of(channel) != SOURCE_CORRECTED:
        return None
    region = table.region(channel)
    attrs = dict(getattr(region.array, "attrs", {}) or {})
    if str(attrs.get("correction_method", "")).lower() != method:
        return None
    try:
        if int(attrs.get("correction_param_value")) != int(param):
            return None
    except (TypeError, ValueError):
        return None
    token = str(attrs.get("source_identity") or "").strip()
    if not token:
        return None
    by0, by1, bx0, bx1 = (int(v) for v in region.bbox)
    if (by0, by1, bx0, bx1) != (0, slide_shape[0], 0, slide_shape[1]):
        return None
    if tuple(int(v) for v in region.array.shape[:2]) != slide_shape:
        return None
    return SavedProduct(table, channel, method, param, token)
