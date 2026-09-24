"""Random patches inside the ROI polygon or the tissue (plan block A2).

Pure numpy / scipy: no Qt, no file IO. The caller reads the nucleus channel at
the level `pick_mask_level` chose and hands the array over.

User rulings (R1, 2026-09-23; constants confirmed 2026-09-24 from the A2
phase-1 measurement on the real slide):

* A patch lies entirely inside the ROI polygon (inside the ROI bbox when the
  ROI has no polygon); without an ROI, anywhere on the slide.
* A candidate whose BLANK share is over 40 % is dropped and another drawn --
  with an ROI as well as without.
* Blank is measured on ONE tissue mask per generation run, computed on the
  pyramid level closest to 16x and not finer. The measurement showed 4x agreed
  with 16x on 99.7 % of accept/reject decisions even for 256 px patches, at
  26x the time (53 s vs 2 s) and 9x the memory.
* The mask is a smoothed, thresholded, closed tissue REGION -- not the nuclei:
  the gaps between nuclei are tissue. Its parameters are in level-0 pixels and
  converted per level; they are empirical values from that slide.

Design choices (plan 4.2): new patches overlap neither each other nor the
existing ones; the seed is fixed by default, so the same request on the same
slide gives the same patches; at most `n * TRIES_PER_PATCH` candidates are
drawn, and a shortfall is reported, never made up by loosening the rules.
"""

from dataclasses import dataclass, field

import numpy as np

TARGET_DS = 16
MAX_BLANK = 0.40
TRIES_PER_PATCH = 200
DEFAULT_SEED = 0
#: Tissue-mask parameters in LEVEL-0 pixels (lengths) and pixels^2 (areas).
PARAMS_L0 = {"sigma": 128.0, "close_r": 256.0, "min_obj": 819_200.0,
             "max_hole": 2_048_000.0}


def pick_mask_level(downsamples, target=TARGET_DS):
    """Index of the pyramid level closest to `target` and not finer than it;
    the coarsest level when every level is finer."""
    ds = [float(d) for d in downsamples]
    if not ds:
        raise ValueError("no pyramid levels")
    coarse_enough = [i for i, d in enumerate(ds) if d >= target - 1e-6]
    if coarse_enough:
        return min(coarse_enough, key=lambda i: ds[i])
    return max(range(len(ds)), key=lambda i: ds[i])


def tissue_mask(signal, ds, params=None):
    """Boolean tissue mask of `signal` (the nucleus channel at downsample `ds`).

    log -> robust [0,1] -> Gaussian -> Otsu -> closing -> fill holes smaller
    than `max_hole` -> drop pieces smaller than `min_obj`. The closing and the
    smoothing bridge the gaps between nuclei, which is what makes this a
    tissue region rather than a nucleus mask.
    """
    from scipy import ndimage as ndi
    from skimage.filters import gaussian, threshold_otsu

    p = dict(PARAMS_L0, **(params or {}))
    ds = float(ds)
    a = np.log1p(np.asarray(signal, dtype=np.float32))
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return np.zeros(a.shape, bool)
    lo, hi = np.percentile(finite, [1, 99.5])
    a = np.clip((np.nan_to_num(a, nan=lo) - lo) / max(hi - lo, 1e-6), 0, 1)
    a = gaussian(a, sigma=p["sigma"] / ds, preserve_range=True)
    if float(a.max()) - float(a.min()) < 1e-6:
        return np.zeros(a.shape, bool)
    fg = a > threshold_otsu(a)
    # Closing by an exact disk, through two distance transforms.
    r = p["close_r"] / ds
    fg = ndi.distance_transform_edt(ndi.distance_transform_edt(~fg) <= r) > r
    holes = ndi.binary_fill_holes(fg) & ~fg
    lab, n = ndi.label(holes)
    if n:
        small = np.zeros(n + 1, bool)
        small[1:] = np.bincount(lab.ravel())[1:] <= p["max_hole"] / (ds * ds)
        fg |= small[lab]
    lab, n = ndi.label(fg)
    if n:
        keep = np.bincount(lab.ravel()) >= p["min_obj"] / (ds * ds)
        keep[0] = False
        fg = keep[lab]
    return fg


def integral_image(mask):
    ii = np.zeros((mask.shape[0] + 1, mask.shape[1] + 1), np.int64)
    ii[1:, 1:] = np.cumsum(np.cumsum(mask.astype(np.int64), 0), 1)
    return ii


def blank_fraction(ii, ds, bbox):
    """Blank share of the level-0 rectangle `bbox` = (y0, y1, x0, x1).

    Area-weighted: each mask cell counts by the part of it the rectangle
    covers, so a rectangle that is not aligned to the mask grid is measured
    by what it actually covers.
    """
    y0, y1, x0, x1 = (float(v) for v in bbox)
    h, w = ii.shape[0] - 1, ii.shape[1] - 1
    fy0, fy1, fx0, fx1 = y0 / ds, y1 / ds, x0 / ds, x1 / ds
    area = (fy1 - fy0) * (fx1 - fx0)
    if area <= 0:
        return 1.0

    def cum(yf, xf):
        # Tissue in [0, yf) x [0, xf) in mask cells, fractional edges included.
        yf, xf = min(max(yf, 0.0), h), min(max(xf, 0.0), w)
        yi, xi = int(np.floor(yf)), int(np.floor(xf))
        ty, tx = yf - yi, xf - xi
        total = float(ii[yi, xi])
        if ty > 0 and yi < h:
            total += ty * float(ii[yi + 1, xi] - ii[yi, xi])
        if tx > 0 and xi < w:
            total += tx * float(ii[yi, xi + 1] - ii[yi, xi])
        if ty > 0 and tx > 0 and yi < h and xi < w:
            cell = float(ii[yi + 1, xi + 1] - ii[yi, xi + 1] - ii[yi + 1, xi] + ii[yi, xi])
            total += ty * tx * cell
        return total

    tissue = cum(fy1, fx1) - cum(fy0, fx1) - cum(fy1, fx0) + cum(fy0, fx0)
    return float(min(1.0, max(0.0, 1.0 - tissue / area)))


def _point_in_polygon(x, y, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        xa, ya = poly[i]
        xb, yb = poly[(i + 1) % n]
        if (ya > y) != (yb > y):
            if x < xa + (y - ya) * (xb - xa) / (yb - ya):
                inside = not inside
    return inside


def _segments_cross(p1, p2, q1, q2):
    """Do the closed segments p1p2 and q1q2 share a point?"""
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 0) - (v < 0)

    def on(a, b, c):
        return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
                and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))

    o1, o2 = orient(p1, p2, q1), orient(p1, p2, q2)
    o3, o4 = orient(q1, q2, p1), orient(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and on(p1, p2, q1)) or (o2 == 0 and on(p1, p2, q2))
            or (o3 == 0 and on(q1, q2, p1)) or (o4 == 0 and on(q1, q2, p2)))


def rect_inside_polygon(bbox, polygon):
    """Is the rectangle `bbox` = (y0, y1, x0, x1) entirely inside `polygon`
    ([(x, y), ...], level-0)? Exact for concave polygons: all four corners
    inside AND no polygon edge touching the rectangle's outline (an edge that
    crosses it would cut a notch out of the rectangle)."""
    y0, y1, x0, x1 = (float(v) for v in bbox)
    poly = [(float(x), float(y)) for x, y in polygon]
    if len(poly) < 3:
        return False
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if not all(_point_in_polygon(x, y, poly) for x, y in corners):
        return False
    sides = list(zip(corners, corners[1:] + corners[:1]))
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        if any(_segments_cross(a, b, c, d) for c, d in sides):
            return False
    return True


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


@dataclass
class Generation:
    """What a run produced, and the record printed for it."""
    patches: list = field(default_factory=list)     # [(y0, y1, x0, x1)]
    requested: int = 0
    tries: int = 0
    seed: int = DEFAULT_SEED
    ds: float = TARGET_DS
    region: tuple = ()
    polygon: bool = False

    @property
    def shortfall(self):
        return self.requested - len(self.patches)

    def record(self):
        return {"requested": self.requested, "found": len(self.patches),
                "tries": self.tries, "seed": self.seed, "mask_ds": self.ds,
                "region": list(self.region), "polygon": self.polygon,
                "max_blank": MAX_BLANK, "params_level0": dict(PARAMS_L0)}


def generate(n, height, width, *, mask, ds, region, polygon=None, existing=(),
             seed=DEFAULT_SEED, max_blank=MAX_BLANK, tries_per_patch=TRIES_PER_PATCH):
    """Draw up to `n` patches of `height` x `width` level-0 pixels.

    `region` = (y0, y1, x0, x1) is where candidates are drawn -- the ROI bbox,
    or the whole slide; `polygon` (level-0 [(x, y), ...]) is what they must
    lie inside when the ROI has one. `mask` is the tissue mask at `ds`.
    """
    height, width = int(height), int(width)
    ry0, ry1, rx0, rx1 = (int(v) for v in region)
    out = Generation(requested=int(n), seed=int(seed), ds=float(ds),
                     region=(ry0, ry1, rx0, rx1), polygon=bool(polygon))
    if n <= 0 or height <= 0 or width <= 0 or ry1 - ry0 < height or rx1 - rx0 < width:
        return out
    ii = integral_image(mask)
    rng = np.random.default_rng(int(seed))
    taken = [tuple(int(v) for v in p) for p in existing]
    budget = int(n) * int(tries_per_patch)
    while len(out.patches) < n and out.tries < budget:
        out.tries += 1
        y0 = int(rng.integers(ry0, ry1 - height + 1))
        x0 = int(rng.integers(rx0, rx1 - width + 1))
        cand = (y0, y0 + height, x0, x0 + width)
        if polygon and not rect_inside_polygon(cand, polygon):
            continue
        if any(_overlap(cand, t) for t in taken):
            continue
        if blank_fraction(ii, ds, cand) > max_blank:
            continue
        out.patches.append(cand)
        taken.append(cand)
    return out
