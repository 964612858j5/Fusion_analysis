"""The automatic core size of Find cores (docs/v17_find_cores_without_map_plan.md
§3.1, §3.4.1): no diameter is given; `dearray` estimates it from the slide
(a rough foreground, a watershed, the area-weighted median) and the
detection then finds every core.

Synthetic slides only (tmp_path), written tile by tile so that a sparse
60 000 x 60 000 slide never sits in memory. Every level is drawn from the
same discs (grainy inside, dark glass outside), scaled to the level.
"""

import math

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")

from block01.core.tma import dearray as da  # noqa: E402
from block01.sources.ome_tiff import OmeTiffSource  # noqa: E402

TILE = 512


def _tile(discs, level, ty, tx, shape, seed):
    """One tile of `level` (factor 2**level): the discs (cy, cx, r in
    level-0 pixels) filled with grain, glass around them."""
    f = 2 ** level
    h, w = shape
    y0, x0 = ty * TILE, tx * TILE
    th, tw = min(TILE, h - y0), min(TILE, w - x0)
    out = np.zeros((TILE, TILE), np.uint8)
    yy = (np.arange(th)[:, None] + y0 + 0.5) * f
    xx = (np.arange(tw)[None, :] + x0 + 0.5) * f
    rng = None
    for cy, cx, r in discs:
        if cy + r < y0 * f or cy - r > (y0 + th) * f or cx + r < x0 * f or cx - r > (x0 + tw) * f:
            continue
        m = (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r
        if m.any():
            rng = rng or np.random.default_rng(seed + 1000003 * level + 7919 * ty + tx)
            out[:th, :tw][m] = rng.uniform(20, 200, int(m.sum())).astype(np.uint8)
    return out


def _write(path, shape0, discs, levels):
    """A pyramidal OME-TIFF (CYX, DAPI + one empty channel), levels by 2."""
    shapes = [(math.ceil(shape0[0] / 2 ** k), math.ceil(shape0[1] / 2 ** k)) for k in range(levels)]

    def tiles(level):
        h, w = shapes[level]
        for c in range(2):
            for ty in range(math.ceil(h / TILE)):
                for tx in range(math.ceil(w / TILE)):
                    yield (_tile(discs, level, ty, tx, (h, w), 11) if c == 0
                           else np.zeros((TILE, TILE), np.uint8))

    opts = dict(tile=(TILE, TILE), compression="zlib", dtype=np.uint8, photometric="minisblack")
    with tifffile.TiffWriter(str(path), bigtiff=True) as tw:
        tw.write(tiles(0), shape=(2, *shapes[0]), subifds=levels - 1,
                 metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "CD8"]}}, **opts)
        for k in range(1, levels):
            tw.write(tiles(k), shape=(2, *shapes[k]), subfiletype=1, metadata=None, **opts)
    return str(path)


def _grid(n_rows, n_cols, d, gap=1.6, margin=1.0):
    return [(margin * d + (i + 0.5) * gap * d, margin * d + (j + 0.5) * gap * d, d / 2)
            for i in range(n_rows) for j in range(n_cols)]


def _canvas(discs, d):
    return (int(max(c[0] for c in discs) + 1.5 * d), int(max(c[1] for c in discs) + 1.5 * d))


def _estimate(path):
    src = OmeTiffSource(path)
    try:
        return da.estimate_core_diameter(src, "DAPI")
    finally:
        src.close()


def _found(cands, discs, tol):
    return all(sum(1 for c in cands if math.hypot(c["cy"] - cy, c["cx"] - cx) <= tol) == 1 for cy, cx, _r in discs)


@pytest.mark.parametrize("d,levels", [(300, 1), (800, 3), (2000, 5)])
def test_the_estimate_fits_cores_of_any_size(tmp_path, d, levels):
    discs = _grid(2, 3, d)
    path = _write(tmp_path / "s.ome.tif", _canvas(discs, d), discs, levels)
    est = _estimate(path)
    assert abs(est - d) <= 0.15 * d, est
    r = da.detect_cores(path, channel="DAPI")
    assert r["core_diameter_source"] == "estimated"
    assert _found(r["candidates"], discs, 0.2 * d)


@pytest.mark.parametrize("n", [1, 2])
def test_one_or_two_cores(tmp_path, n):
    d = 400
    discs = _grid(1, n, d)
    path = _write(tmp_path / "s.ome.tif", _canvas(discs, d), discs, 2)
    assert abs(_estimate(path) - d) <= 0.15 * d
    assert _found(da.detect_cores(path, channel="DAPI")["candidates"], discs, 0.2 * d)


@pytest.mark.parametrize("ratio", [1.0, 0.7])
def test_touching_cores_are_split_and_do_not_drag_the_size(tmp_path, ratio):
    """Six single cores; below the first two, a second core overlapping it
    (centres 0.9 of the two radii apart): equal, then 1 : 0.7."""
    d = 400
    singles = _grid(2, 3, d, gap=2.0)
    partners = [(cy + 0.9 * (r + r * ratio), cx, r * ratio) for cy, cx, r in singles[:2]]
    discs = singles + partners
    path = _write(tmp_path / "s.ome.tif", _canvas(discs, d), discs, 2)
    est = _estimate(path)
    assert abs(est - d) <= 0.15 * d, est
    cands = da.detect_cores(path, channel="DAPI")["candidates"]
    assert _found(cands, singles[2:], 0.25 * d)
    # the estimate decides the split exactly as the true size would: the
    # equal pairs are split; a deeply overlapping 1 : 0.7 pair is one piece
    # with either (solidity above `split_solidity`: today's detection rule)
    given = da.detect_cores(path, channel="DAPI", core_diameter_px=d)["candidates"]
    assert len(cands) == len(given)
    for g in given:                                       # the same pieces, within a few pixels
        same = [c for c in cands if math.hypot(c["cy"] - g["cy"], c["cx"] - g["cx"]) <= 5]
        assert len(same) == 1 and same[0]["split"] == g["split"], g
    if ratio == 1.0:
        assert _found(cands, singles + partners, 0.25 * d)    # each core of the equal pairs on its own


@pytest.mark.parametrize("share", [0.10, 0.30])
def test_fragments_count_by_their_area_not_their_number(tmp_path, share):
    """More fragments than cores, holding `share` of the tissue area: the
    area-weighted median still lands on the cores."""
    d = 500
    cores = _grid(2, 2, d, gap=2.2)
    fd = 0.15 * d                                         # a fragment's diameter
    n_frag = int(round(share / (1 - share) * len(cores) * (d / fd) ** 2))
    assert n_frag > len(cores)
    rng = np.random.default_rng(3)
    shape = _canvas(cores, d)
    frags = []
    while len(frags) < n_frag:
        cy, cx = rng.uniform(0.3 * d, shape[0] - 0.3 * d), rng.uniform(0.3 * d, shape[1] - 0.3 * d)
        if all(math.hypot(cy - a, cx - b) > r + fd for a, b, r in cores + frags):
            frags.append((cy, cx, fd / 2))
    path = _write(tmp_path / "s.ome.tif", shape, cores + frags, 2)
    est = _estimate(path)
    assert abs(est - d) <= 0.15 * d, est
    assert _found(da.detect_cores(path, channel="DAPI")["candidates"], cores, 0.2 * d)


def test_a_sparse_large_slide_keeps_its_cores(tmp_path):
    """4 cores of 2000 px on 60 000 x 60 000 px (each under 0.1 % of the
    image): an image-relative speck size would drop them all."""
    d = 2000
    discs = [(15000, 15000, d / 2), (15000, 45000, d / 2), (45000, 15000, d / 2), (45000, 45000, d / 2)]
    path = _write(tmp_path / "big.ome.tif", (60000, 60000), discs, 6)
    assert abs(_estimate(path) - d) <= 0.15 * d
    assert _found(da.detect_cores(path, channel="DAPI")["candidates"], discs, 0.2 * d)


@pytest.mark.parametrize("shape,d", [((600, 800), 150), ((12000, 9000), 1500)])
def test_slides_without_a_pyramid_are_sampled_at_most_2048(tmp_path, shape, d):
    discs = _grid(2, 2, d, margin=0.6)
    path = _write(tmp_path / "flat.ome.tif", shape, discs, 1)
    src = OmeTiffSource(path)
    try:
        shapes = [src.level_shape(lv) for lv in range(src.level_count())]
    finally:
        src.close()
    lv = da.estimate_level(shapes)
    assert lv == 0
    assert min(shapes[lv]) // da.block_factor(shapes[lv]) <= da.ESTIMATE["max_short_px"]
    assert abs(_estimate(path) - d) <= 0.15 * d


def test_a_blank_slide_says_there_is_no_tissue(tmp_path):
    path = _write(tmp_path / "blank.ome.tif", (1500, 1500), [], 2)
    with pytest.raises(da.CoreSizeError, match="No tissue found"):
        da.detect_cores(path, channel="DAPI")


def test_the_weighted_median_and_its_ties():
    assert da._weighted_median([1, 10], [1, 1]) == 1            # a tie goes to the smaller
    assert da._weighted_median([1, 10], [1, 3]) == 10
    assert da._weighted_median([5, 1, 3], [1, 1, 1]) == 3
