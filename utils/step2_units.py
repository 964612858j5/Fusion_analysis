"""Step2's units for a run that follows cores (docs/v17_step2_core_by_core_plan.md
§3.1): one unit per selected core instead of a tile grid.

* A unit is the core's bbox, clipped to the region, in region-local
  half-open pixels; the read window IS that bbox (no halo: Find cores'
  outlines already reach beyond the tissue). It owns its whole window
  (`own_bbox = read_bbox`), so every whole object in it is owned; units
  whose windows intersect resolve their shared objects as tile seams.
* A core whose window is larger than the engine takes in one call is split
  into the fewest equal parts of its bbox (rows x cols, rows <= cols, fewer
  rows on a tie); the parts overlap by `part_overlap` -- only inside the
  core's bbox -- and each owns its whole window too.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

from .step2_tile import Step2Tile

#: one engine call's read area, per engine family (plan §3.1)
CAP_PX = {"stardist": 60_000_000, "mesmer": 35_000_000, "cellpose": 64_000_000}


#: StarDist on a CUDA GPU (docs/v17_stardist_vram_tile_plan.md §2.1): the
#: network's peak per read pixel (the memory-lean graph, §2.5; measured 192.1
#: B/px allocated on the 6 GiB laptop, 2026-11-19), the share of the card planned for, and
#: what stays taken besides the tile (CUDA context, weights, cuDNN workspace;
#: the hybrid also keeps Cellpose's weights).
STARDIST_BYTES_PER_PX = 200
VRAM_USABLE = 0.75
CUDA_RESERVE_GIB = 1.0
HYBRID_RESERVE_GIB = 1.5
STARDIST_MIN_CAP_PX = 1_000_000


def stardist_cap_px(method: str, vram_gib, use_gpu: bool) -> int:
    """One StarDist engine call's read area limit: the host-memory cap, and on
    a CUDA card of `vram_gib` (None: unknown, or not CUDA) also what its
    memory holds."""
    cap = CAP_PX["stardist"]
    if not use_gpu or not vram_gib:
        return cap
    reserve = CUDA_RESERVE_GIB + (HYBRID_RESERVE_GIB if "hybrid" in str(method or "").lower() else 0.0)
    budget = (float(vram_gib) * VRAM_USABLE - reserve) * 1024.0 ** 3
    return int(min(cap, max(STARDIST_MIN_CAP_PX, budget / STARDIST_BYTES_PER_PX)))


def engine_cap_px(method: str, stardist_cap=None) -> int:
    """`stardist_cap`: the run's StarDist limit (`stardist_cap_px` in the run
    configuration, decided in the GUI); None = the host-memory cap."""
    m = str(method or "").lower()
    if "stardist" in m:                     # StarDist and the hybrid's StarDist part
        return int(stardist_cap) if stardist_cap else CAP_PX["stardist"]
    if "mesmer" in m:
        return CAP_PX["mesmer"]
    return CAP_PX["cellpose"]


def _parts(box: Tuple[int, int, int, int], rows: int, cols: int, overlap: int):
    """(rows x cols) equal parts of `box`, each grown by `overlap` inside the box."""
    y0, y1, x0, x1 = box
    h, w = y1 - y0, x1 - x0
    ph, pw = -(-h // rows), -(-w // cols)
    out = []
    for r in range(rows):
        for c in range(cols):
            oy0, ox0 = y0 + r * ph, x0 + c * pw
            oy1, ox1 = min(oy0 + ph, y1), min(ox0 + pw, x1)
            if oy1 <= oy0 or ox1 <= ox0:
                continue
            ry0 = max(y0, oy0 - (overlap if r else 0))
            ry1 = min(y1, oy1 + (overlap if r < rows - 1 else 0))
            rx0 = max(x0, ox0 - (overlap if c else 0))
            rx1 = min(x1, ox1 + (overlap if c < cols - 1 else 0))
            out.append(((oy0, oy1, ox0, ox1), (ry0, ry1, rx0, rx1)))
    return out


def split_grid(box, cap_px: int, overlap: int) -> Tuple[int, int]:
    """The fewest parts (rows, cols) whose read windows all fit `cap_px`:
    rows <= cols, fewer rows on a tie."""
    y0, y1, x0, x1 = box
    for n in range(1, 10_000):
        for rows in range(1, int(math.isqrt(n)) + 1):
            if n % rows:
                continue
            cols = n // rows
            parts = _parts(box, rows, cols, overlap)
            if parts and all((b[1] - b[0]) * (b[3] - b[2]) <= cap_px for _o, b in parts):
                return rows, cols
    raise ValueError(f"a core of {y1 - y0} x {x1 - x0} px cannot be split under {cap_px} px")


def core_units(snapshot: Dict, region_bbox: Sequence[int], method: str, part_overlap: int,
               out_prefix: str = "", stardist_cap=None) -> List[Step2Tile]:
    """The units of a cores run over the region `region_bbox` (global
    [y0, y1, x0, x1]), in the snapshot's core order; region-local boxes.
    `stardist_cap`: the run's StarDist limit (see `engine_cap_px`)."""
    gy0, gy1, gx0, gx1 = [int(v) for v in region_bbox]
    cap = engine_cap_px(method, stardist_cap)
    units: List[Step2Tile] = []
    shape = tuple(int(v) for v in snapshot.get("slide_shape") or (gy1, gx1))
    for c in snapshot.get("cores") or []:
        bb = c.get("bbox")
        if bb is None:                                      # an older / hand-made record: from its outline
            from ..core.regions.geometry import bbox_of
            bb = bbox_of(c["polygon"], shape)
        by0, by1, bx0, bx1 = [int(v) for v in bb]
        box = (max(by0, gy0) - gy0, min(by1, gy1) - gy0, max(bx0, gx0) - gx0, min(bx1, gx1) - gx0)
        if box[1] <= box[0] or box[3] <= box[2]:
            continue                                        # not in this region
        area = (box[1] - box[0]) * (box[3] - box[2])
        rows, cols = (1, 1) if area <= cap else split_grid(box, cap, int(part_overlap))
        parts = _parts(box, rows, cols, int(part_overlap)) if (rows, cols) != (1, 1) else [(box, box)]
        for j, (cell, win) in enumerate(parts):
            units.append(Step2Tile(index=len(units), row=len(units), col=0, own_bbox=win, read_bbox=win,
                                   overlap=int(part_overlap) if len(parts) > 1 else 0,
                                   out_prefix=out_prefix, core_uid=str(c.get("uid", "")),
                                   part=(j, len(parts))))
    return units
