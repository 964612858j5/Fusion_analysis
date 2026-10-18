"""TMA core detection (F2, docs/tma_foundation_application.md §4 F2, as the
user narrowed it 2026-10-17): find the tissue foreground and the candidate
cores, and measure each one. Nothing more is product infrastructure here:
matching candidates to map-sheet positions, the slide's orientation and
any position status are NOT done by this module (a one-off analysis does
them for now; the TMA page lets the user do them, F3).

* Foreground by texture: the local standard deviation of the log nucleus
  signal at a coarse level (nuclei make tissue grainy, even in a dim core;
  glass and the smooth glow between scan fields are not), smoothed, a
  threshold between the glass level and Otsu's, a small closing (bridges
  the gaps inside a core, far smaller than the gap between cores), holes
  filled, an opening that drops dust. Parameters are TMA's own, scaled by
  the expected core diameter -- not `core.random_patches.tissue_mask`'s,
  which are tuned for one large block and would bridge neighbouring cores.
* Candidates: connected foreground pieces of at least `min_area_frac` of
  a core; a piece much larger than one core, or somewhat larger and clearly
  not convex (touching cores), is split by a watershed on its distance
  transform, seeded a fraction of a core radius apart.
* Outlines (format 2, Regions plan §3): each piece's mask grown by
  `margin_frac` of a core diameter; the grown ring is given to the nearest
  piece, and only in that ring a one-pixel gap is kept where two pieces
  meet -- the pieces themselves are never trimmed. The outer contour runs
  through the centres of the boundary pixels, is simplified by half a
  pixel and mapped to whole level-0 pixels (A3 `level_to_global`).
* Measures, in level-0 pixels: centre, equivalent radius, area, the
  circularity 4*pi*A/P**2, the solidity, whether it touches the slide's
  edge, and its tissue fraction (foreground share of the expected-size
  disc at its centre). These are measurements, not a verdict.

Pixels are read through `OmeTiffSource` (the PixelSource contract). The
candidate list is saved as ``<project>/tma/dearray_candidates_v<n>.json``
(n counts up, nothing is overwritten) and registered as provenance kind
``tma_dearray``.

Known limit: at a coarse level, a core cut by a slide CORNER (centre
closer than about one radius to both edges) can be dropped; at level 0 such
a core is found and marked ``touches_edge``. Cores that sit on a slide
corner are rare; the TMA page's manual correction (F3) covers them.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

SCHEMA_VERSION = 2            # 2: each candidate has an ``outline`` (Regions R1); 1 is still read
READABLE_SCHEMAS = (1, 2)
PARAMS = {
    "min_core_px": 80,        # choose the coarsest level where a core is at least this wide
    "texture_frac": 0.06,     # local standard deviation window, as a fraction of the core diameter
    "sigma_frac": 0.03,       # Gaussian smoothing of the texture
    "thr_frac": 0.8,          # threshold between the glass level and Otsu's (0.8 fits TMA138 Slice1)
    "close_frac": 0.04,       # closing radius
    "open_frac": 0.03,        # opening radius
    "min_area_frac": 0.01,    # smallest piece kept (before the margin), as a fraction of a core's area:
                              # fragments are kept for the user to merge (Regions R1; was 0.10)
    "margin_frac": 0.06,      # outlines grow by this many core diameters: larger than the tissue (user 2026-10-18: 3% too tight)
    "split_area_frac": 1.6,   # a piece larger than this many cores is split ...
    "split_concave_area_frac": 1.15,  # ... and so is one above this many cores that is
    "split_solidity": 0.85,           #     clearly not convex (two discs joined)
    "seed_sep_frac": 0.6,     # watershed seeds at least this many radii apart
}


def choose_level(level_shapes, core_diameter_l0: float, min_core_px: float) -> int:
    """The coarsest level where a core is still `min_core_px` wide."""
    h0 = level_shapes[0][0]
    best = 0
    for lv, (h, _w) in enumerate(level_shapes):
        if core_diameter_l0 * h / h0 >= min_core_px:
            best = lv
    return best


def foreground(signal: np.ndarray, diameter_px: float, params: Dict) -> np.ndarray:
    """Tissue by TEXTURE, not brightness: the local standard deviation of
    the log signal (nuclei make tissue grainy, even in a dim core; glass and
    the smooth glow between scan fields are not), smoothed, thresholded
    `thr_frac` of the way from the glass level up to Otsu's."""
    from scipy import ndimage as ndi
    from skimage.filters import threshold_otsu
    a = np.log1p(np.nan_to_num(np.asarray(signal, dtype=np.float32), nan=0.0))
    w = max(3, int(round(params["texture_frac"] * diameter_px)) | 1)
    m = ndi.uniform_filter(a, w)
    t = np.sqrt(np.maximum(ndi.uniform_filter(a * a, w) - m * m, 0.0))
    t = ndi.gaussian_filter(t, max(params["sigma_frac"] * diameter_px, 0.5))
    if float(t.max()) - float(t.min()) < 1e-6:
        return np.zeros(t.shape, bool)
    bg, th = float(np.percentile(t, 20)), float(threshold_otsu(t))
    fg = t > bg + params["thr_frac"] * max(th - bg, 0.0)
    r = params["close_frac"] * diameter_px
    fg = ndi.distance_transform_edt(ndi.distance_transform_edt(~fg) <= r) > r          # closing
    fg = ndi.binary_fill_holes(fg)
    # the texture window and its smoothing widen every boundary by about
    # half the window plus the sigma: take that back, then open
    grow = w / 2 + max(params["sigma_frac"] * diameter_px, 0.5)
    r = params["open_frac"] * diameter_px
    fg = ndi.distance_transform_edt(fg) > grow + r                                       # shrink + opening ...
    fg = ndi.distance_transform_edt(~fg) <= r                                            # ... by a disc
    return fg


def _pieces(fg: np.ndarray, diameter_px: float, params: Dict):
    """Labels of the candidate pieces, touching cores split."""
    from scipy import ndimage as ndi
    from skimage.feature import peak_local_max
    from skimage.segmentation import watershed
    core_area = np.pi * (diameter_px / 2) ** 2
    lab, n = ndi.label(fg)
    out = np.zeros(fg.shape, np.int32)
    split_of = {}
    nxt = 0
    for i, sl in enumerate(ndi.find_objects(lab), start=1):
        if sl is None:
            continue
        piece = lab[sl] == i
        area = int(piece.sum())
        if area < params["min_area_frac"] * core_area:
            continue
        from skimage.morphology import convex_hull_image
        concave = (area > params["split_concave_area_frac"] * core_area
                   and area < params["split_solidity"] * float(convex_hull_image(piece).sum()))
        if area > params["split_area_frac"] * core_area or concave:
            dist = ndi.distance_transform_edt(piece)
            seeds = peak_local_max(dist, min_distance=max(int(params["seed_sep_frac"] * diameter_px / 2), 1),
                                   labels=piece.astype(np.int32), exclude_border=False)
            if len(seeds) > 1:
                markers = np.zeros(piece.shape, np.int32)
                for k, (y, x) in enumerate(seeds, start=1):
                    markers[y, x] = k
                parts = watershed(-dist, markers, mask=piece)
                for k in range(1, len(seeds) + 1):
                    m = parts == k
                    if m.sum() >= params["min_area_frac"] * core_area:
                        nxt += 1
                        out[sl][m] = nxt
                        split_of[nxt] = i
                continue
        nxt += 1
        out[sl][piece] = nxt
    return out, nxt, split_of


def _perimeter(mask: np.ndarray) -> float:
    from skimage.measure import perimeter
    return float(perimeter(mask))


def measure(fg: np.ndarray, labels: np.ndarray, n: int, split_of: Dict, diameter_px: float,
            scale_yx, to_level0=None) -> List[Dict]:
    """One record per piece, in level-0 pixels (`scale_yx`: level-0 pixels
    per pixel of this level; `to_level0(x, y)`: a pixel centre of this level
    to level 0, A3's `SlideFrames.level_to_global` -- not a bare multiply)."""
    if to_level0 is None:
        to_level0 = lambda x, y: ((x + 0.5) * scale_yx[1] - 0.5, (y + 0.5) * scale_yx[0] - 0.5)  # noqa: E731
    from scipy import ndimage as ndi
    from skimage.morphology import convex_hull_image
    sy, sx = scale_yx
    h, w = fg.shape
    r_exp = diameter_px / 2
    yy, xx = np.mgrid[-int(np.ceil(r_exp)):int(np.ceil(r_exp)) + 1, -int(np.ceil(r_exp)):int(np.ceil(r_exp)) + 1]
    disc = (yy ** 2 + xx ** 2) <= r_exp ** 2
    out = []
    for k, sl in enumerate(ndi.find_objects(labels), start=1):
        if sl is None:
            continue
        m = labels[sl] == k
        area = float(m.sum())
        cy, cx = ndi.center_of_mass(m)
        cy, cx = cy + sl[0].start, cx + sl[1].start
        per = _perimeter(np.pad(m, 1))
        hull = float(convex_hull_image(m).sum()) if m.any() else 0.0
        touches = sl[0].start == 0 or sl[1].start == 0 or sl[0].stop >= h or sl[1].stop >= w
        # tissue fraction: foreground share of the expected disc at the centre (inside the image)
        y0, x0 = int(round(cy)) - disc.shape[0] // 2, int(round(cx)) - disc.shape[1] // 2
        ya, xa = max(y0, 0), max(x0, 0)
        yb, xb = min(y0 + disc.shape[0], h), min(x0 + disc.shape[1], w)
        d = disc[ya - y0:yb - y0, xa - x0:xb - x0]
        tissue = float(fg[ya:yb, xa:xb][d].mean()) if d.any() else 0.0
        gx, gy = to_level0(cx, cy)
        out.append({
            "candidate_id": len(out) + 1,
            "cy": round(float(gy), 1), "cx": round(float(gx), 1),
            "radius": round(float(np.sqrt(area * sy * sx / np.pi)), 1),
            "area": round(area * sy * sx, 1),
            "circularity": round(4 * np.pi * area / per ** 2, 4) if per > 0 else 0.0,
            "solidity": round(area / hull, 4) if hull > 0 else 0.0,
            "touches_edge": bool(touches),
            "tissue_fraction": round(tissue, 4),
            "split": k in split_of,
        })
    return out


def grown_parts(labels: np.ndarray, margin: int) -> np.ndarray:
    """Each piece grown by `margin` pixels: a grown pixel goes to the
    nearest piece; a grown pixel touching another piece's pixel is dropped
    (the gap). Pixels of the pieces themselves keep their label."""
    from scipy import ndimage as ndi
    if not labels.any():
        return labels.copy()
    dist, (iy, ix) = ndi.distance_transform_edt(labels == 0, return_indices=True)
    near = labels[iy, ix]
    grown = np.where(dist <= margin, near, 0).astype(labels.dtype)
    added = (labels == 0) & (grown > 0)
    pad = np.pad(grown, 1)
    h, w = grown.shape
    clash = np.zeros(grown.shape, bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                nb = pad[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
                clash |= (nb > 0) & (nb != grown)
    grown[added & clash] = 0
    return grown


def outlines(labels: np.ndarray, n: int, diameter_px: float, params: Dict, to_level0, shape0) -> List[List[List[int]]]:
    """One outline per piece (in label order 1..n), level-0 whole pixels
    ``[[x, y], ...]``, clipped to the slide."""
    import cv2
    margin = max(1, int(round(params["margin_frac"] * diameter_px)))
    grown = grown_parts(labels, margin)
    h0, w0 = shape0
    out = []
    for k in range(1, n + 1):
        m = (grown == k).astype(np.uint8)
        found, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not found:
            out.append([])
            continue
        c = max(found, key=cv2.contourArea)
        c = cv2.approxPolyDP(c, 0.5, True)
        poly = []
        for q in c[:, 0, :]:
            gx, gy = to_level0(float(q[0]), float(q[1]))
            xy = [min(max(int(round(gx)), 0), w0 - 1), min(max(int(round(gy)), 0), h0 - 1)]
            if not poly or poly[-1] != xy:
                poly.append(xy)
        out.append(poly)
    return out


def detect_cores(slide_path: str, *, core_diameter_um: Optional[float] = None,
                 core_diameter_px: Optional[float] = None, channel=0, level: Optional[int] = None,
                 params: Optional[Dict] = None) -> Dict:
    """Candidate cores of the slide at `slide_path`. The expected core size
    is `core_diameter_um` (needs the slide's pixel size) or
    `core_diameter_px` (level-0 pixels). `channel`: the nucleus channel
    (index or name)."""
    from ...sources.ome_tiff import OmeTiffSource
    from ..project_identity import SlideFrames
    p = dict(PARAMS, **(params or {}))
    bad = [k for k, v in p.items() if not (isinstance(v, (int, float)) and np.isfinite(v) and v > 0)]
    if bad:
        raise ValueError(f"parameters must be positive and finite: {bad}")
    for name, v in (("core_diameter_px", core_diameter_px), ("core_diameter_um", core_diameter_um)):
        if v is not None and not (np.isfinite(v) and v > 0):
            raise ValueError(f"{name} must be positive and finite, not {v!r}")
    src = OmeTiffSource(slide_path)
    try:
        shapes = [src.level_shape(lv) for lv in range(src.level_count())]
        pixel = src.physical_size()
        if core_diameter_px is None:
            if core_diameter_um is None or not pixel:
                raise ValueError("the core size needs core_diameter_px, or core_diameter_um and a pixel size")
            core_diameter_px = float(core_diameter_um) / float((pixel[0] + pixel[1]) / 2)
        lv = choose_level(shapes, core_diameter_px, p["min_core_px"]) if level is None else int(level)
        h, w = shapes[lv]
        signal, _origin = src.read_region(channel, lv, 0, h, 0, w)
        name = src.channel_names()[channel] if isinstance(channel, int) else str(channel)
    finally:
        src.close()
    frames = SlideFrames(shapes)
    scale = frames.scale_yx(lv)
    d_lv = core_diameter_px / float(np.sqrt(scale[0] * scale[1]))   # cores are round; levels are ~isotropic
    fg = foreground(signal, d_lv, p)
    labels, n, split_of = _pieces(fg, d_lv, p)
    to0 = lambda x, y: frames.level_to_global(x, y, lv)       # noqa: E731
    cands = measure(fg, labels, n, split_of, d_lv, scale, to_level0=to0)
    for c, poly in zip(cands, outlines(labels, n, d_lv, p, to0, shapes[0])):
        c["outline"] = poly
    return {
        "schema_version": SCHEMA_VERSION,
        "slide_path": os.path.abspath(slide_path),
        "channel": name, "level": lv, "level_shape": [h, w], "scale_yx": [round(s, 6) for s in scale],
        "pixel_um": list(pixel) if pixel else None,
        "core_diameter_px": round(float(core_diameter_px), 2),
        "params": p, "candidates": cands,
    }


def candidates_path(project_dir, version: int) -> str:
    from .study import STUDY_DIR
    return os.path.join(project_dir, STUDY_DIR, f"dearray_candidates_v{int(version)}.json")


def next_version(project_dir) -> int:
    from .study import STUDY_DIR
    d = os.path.join(project_dir, STUDY_DIR)
    taken = []
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.startswith("dearray_candidates_v") and f.endswith(".json"):
                try:
                    taken.append(int(f[len("dearray_candidates_v"):-len(".json")]))
                except ValueError:
                    pass
    return max(taken, default=0) + 1


def save_candidates(project_dir, result: Dict, slide_id: str) -> Dict:
    """Write `result` as the next version and register it (``tma_dearray``,
    depending on the slide's ``raw_slide`` entry). Returns the record; its
    ``artifact_id`` (None when the provenance did not take it) is in the
    returned record only, not in the file, which is written first."""
    from .. import provenance as prov
    version = next_version(project_dir)
    rec = dict(result, version=version, slide_id=slide_id,
               created_at=datetime.now().isoformat(timespec="seconds"))
    path = candidates_path(project_dir, version)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):                                   # never overwrite
        raise FileExistsError(path)
    prov.write_json_atomic(path, rec)
    token = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    aid = prov.safe_register(project_dir, "tma_dearray", prov.location(project_dir, path), token,
                       slide_id=slide_id, depends_on=[slide_id],
                       parameters={"version": version, "level": result["level"], "channel": result["channel"],
                                   "core_diameter_px": result["core_diameter_px"],
                                   "candidates": len(result["candidates"]), "params": result["params"]},
                       software={"module": "core.tma.dearray", "commit": prov.git_commit()})
    if aid is None:                                            # saved, but the provenance did not take it
        print(f"[TMA] {path} saved but NOT registered in the provenance")
    rec["artifact_id"] = aid
    return rec


def load_candidates(project_dir, version: int) -> Dict:
    with open(candidates_path(project_dir, version), "r", encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("schema_version") not in READABLE_SCHEMAS:
        raise ValueError(f"dearray candidates v{version}: schema_version {rec.get('schema_version')!r}")
    return rec
