"""The cell rules of the analysis (docs/f4_regions_downstream_plan.md v2
§B.3-§B.4, user 2026-10-18): which segmented objects are written and
with what core and quality facts. No Qt, no I/O.

An object is counted window by window (`ObjectTally.add`, any tiling: an
object spanning windows is summed), then `ObjectTally.verdicts` decides:

* core -- the core holding MORE than half of the object's pixels (so at
  most one); otherwise none and the object is dropped ("no_core");
* any pixel inside a segmentation-invalid exclusion -- dropped
  ("exclusion");
* kept objects carry `core_fraction`, `touches_core_edge` (pixels outside
  their core) and `analysis_include` (wholly inside it);
* measurement-only exclusions: the channels of every exclusion the object
  overlaps by at least one pixel are invalid for it (NaN in Step4, code
  `MEASUREMENT_EXCLUDED` in ``layers["measurement_qc"]``).

Counts must come from the uncut predicted object (before seam clipping,
before Step1 crops a result to its patch) for the fractions to mean
anything: `evaluate_object` judges one original object mask at its
global place, and the caller carries the verdict to the object's final
id. Dropped objects never get a published id, so published ids stay
1..N. A nucleus follows the cell it belongs to; in a nuclei-only run the
nucleus is the object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import numpy as np

MIN_CORE_FRACTION = 0.5          # strictly more than this share of the object
MEASUREMENT_VALID = 0
MEASUREMENT_EXCLUDED = 1          # overlaps a measurement-only exclusion of that channel
MEASUREMENT_QC_CODES = {MEASUREMENT_VALID: "valid",
                        MEASUREMENT_EXCLUDED: "overlaps an exclusion of this channel"}
DROP_REASONS = ("no_core", "outside_region", "exclusion")


@dataclass
class Verdict:
    keep: bool
    reason: str                           # "" when kept, else one of DROP_REASONS
    core: Optional[int]                   # index into snapshot["cores"], None when dropped
    core_fraction: float
    touches_core_edge: bool
    analysis_include: bool                # wholly inside its core (says nothing about the parent edge)
    excluded_channels: Set[str] = field(default_factory=set)
    touches_region_edge: bool = False     # reaches the parent region's border or outline (may be cut there)


class ObjectTally:
    """Per-object pixel counts over any number of windows. `tma`: False for a
    snapshot without cores (the domain is the parent region; a kept object
    then has no core)."""

    def __init__(self, tma: bool = True):
        self.tma = tma
        self.area: Dict[int, int] = {}
        self.core: Dict[int, Dict[int, int]] = {}
        self.seg: Dict[int, int] = {}
        self.meas: Dict[int, Set[int]] = {}
        self.meas_channels: Dict[int, List[str]] = {}

    def add(self, labels: np.ndarray, core_index: np.ndarray, seg_invalid: np.ndarray,
            measurement=()) -> None:
        """One window: `labels` (0 = background), the raster's core index,
        its segmentation-invalid mask and its measurement exclusions
        (``AnalysisRaster`` gives all three for the same window)."""
        fg = labels > 0
        if not fg.any():
            return
        lab = labels[fg].astype(np.int64)
        ids, n = np.unique(lab, return_counts=True)
        for i, c in zip(ids.tolist(), n.tolist()):
            self.area[i] = self.area.get(i, 0) + c
        pair = lab * 65536 + core_index[fg].astype(np.int64)
        pids, pn = np.unique(pair, return_counts=True)
        for p, c in zip(pids.tolist(), pn.tolist()):
            i, k = divmod(p, 65536)
            if k:
                d = self.core.setdefault(i, {})
                d[k - 1] = d.get(k - 1, 0) + c
        if seg_invalid.any():
            sids, sn = np.unique(labels[fg & seg_invalid], return_counts=True)
            for i, c in zip(sids.tolist(), sn.tolist()):
                self.seg[i] = self.seg.get(i, 0) + c
        for ex, chans, mask in measurement:
            self.meas_channels[ex] = list(chans)
            hit = labels[fg & mask]
            for i in np.unique(hit).tolist():
                self.meas.setdefault(i, set()).add(ex)

    def verdicts(self) -> Dict[int, Verdict]:
        out = {}
        for i, area in self.area.items():
            counts = self.core.get(i, {})
            core, best = max(counts.items(), key=lambda kv: kv[1]) if counts else (None, 0)
            frac = best / area if area else 0.0
            if core is None or frac <= MIN_CORE_FRACTION:
                out[i] = Verdict(False, "no_core" if self.tma else "outside_region", None, frac, True, False)
            elif self.seg.get(i):
                out[i] = Verdict(False, "exclusion", None, frac, frac < 1.0, False)
            else:
                chans = {c for ex in self.meas.get(i, ()) for c in self.meas_channels.get(ex, ())}
                out[i] = Verdict(True, "", core if self.tma else None, frac, frac < 1.0, frac >= 1.0, chans)
        return out


def measurement_qc(verdicts: Dict[int, Verdict], ids: List[int], channels: List[str]) -> np.ndarray:
    """The cells x channels code matrix (`MEASUREMENT_*`) for `ids` in order;
    a channel listed by an exclusion but absent from `channels` is ignored."""
    col = {c: j for j, c in enumerate(channels)}
    out = np.full((len(ids), len(channels)), MEASUREMENT_VALID, dtype=np.uint8)
    for r, i in enumerate(ids):
        v = verdicts.get(i)
        if v is None:
            continue
        for c in v.excluded_channels:
            if c in col:
                out[r, col[c]] = MEASUREMENT_EXCLUDED
    return out


def drop_counts(verdicts: Dict[int, Verdict]) -> Dict[str, int]:
    out = {r: 0 for r in DROP_REASONS}
    for v in verdicts.values():
        if not v.keep:
            out[v.reason] += 1
    return out


def evaluate_object(mask: np.ndarray, y0: int, x0: int, raster, region_bbox=None) -> Verdict:
    """The verdict of ONE original object: `mask` (bool) placed with its top
    left at level-0 (`y0`, `x0`), judged against `raster`
    (`raster.AnalysisRaster`); `region_bbox` ``[y0, y1, x0, x1]`` of the
    parent region marks objects reaching its border."""
    h, w = mask.shape
    win = (int(y0), int(y0) + h, int(x0), int(x0) + w)
    t = ObjectTally(tma=raster.tma)
    t.add(mask.astype(np.int32), raster.core_index(win), raster.seg_invalid(win),
          raster.measurement_exclusions(win))
    v = t.verdicts().get(1) or Verdict(False, "no_core", None, 0.0, True, False)
    if region_bbox is not None and mask.any():
        ys, xs = np.nonzero(mask)
        ry0, ry1, rx0, rx1 = region_bbox
        at_box = (ys.min() + y0 <= ry0 or ys.max() + y0 >= ry1 - 1
                  or xs.min() + x0 <= rx0 or xs.max() + x0 >= rx1 - 1)
        par = raster.parent.window(win)                 # a sloping ROI edge counts too
        v.touches_region_edge = bool(at_box or (mask & ~par).any())
    return v
