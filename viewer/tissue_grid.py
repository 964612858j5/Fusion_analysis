"""The Tissue Preview's one grid, and its SAVED-stage pixels.

WHY THIS EXISTS (CD45RO, TMA4, 2026-10-11). Step1/Step2/Step3 drew their
Tissue Preview from Step0's whole-slide arrays -- the RAW slide -- through
the display window the user set on the CORRECTED product. A channel saved as
`tophat` therefore showed its whole uncorrected background in the thumbnail
while the viewer beside it showed the corrected one: measured over the
tissue at the overview level, 18.6 % of the pixels above a quarter of the
window raw, 0.7 % corrected.

Two answers live here, both pure functions so they can run on a worker:

* `tissue_grid` / `to_tissue_grid` -- ONE grid for every array of a frame.
  Step0's arrays come from two producers that disagree: a viewer's overview
  record is a pyramid level (1462x840 on that slide, stride 32), the loader
  fallback is `overview_downsample()` (732x420, stride 64). A frame mixing
  them dropped layers on the shape check -- the nucleus among them. The grid
  is the loader's: stride `overview_downsample()`, blocks anchored at the
  level-0 origin, `ceil(H / stride)` rows -- exactly what
  `read_region_lowres(..., ds)` returns -- and a finer array is decimated to
  it with the same floor mapping.
* `corrected_overview` -- a corrected channel on that grid, from its SAVED
  product: the persisted coarse plane when one matches (Step1's own checks,
  `Step1SourceTable.coarse_plane`), otherwise the bounded runtime reduction.
  Never the raw slide: a channel whose product is missing has no picture
  rather than a raw one dressed as corrected.
"""

import numpy as np

from .step1_source import SOURCE_CORRECTED, reduce_corrected


def tissue_grid(level0_shape, stride):
    """`(rows, cols)` of the Tissue Preview grid at `stride`."""
    h, w = (int(v) for v in level0_shape[:2])
    stride = max(1, int(stride))
    return -(-h // stride), -(-w // stride)


def _fit(arr, shape):
    """Crop, or repeat the last row/column, to exactly `shape`."""
    rows, cols = shape
    arr = arr[:rows, :cols]
    pad_r, pad_c = rows - arr.shape[0], cols - arr.shape[1]
    if pad_r > 0 or pad_c > 0:
        if arr.shape[0] == 0 or arr.shape[1] == 0:
            return np.zeros(shape, np.float32)
        arr = np.pad(arr, ((0, max(0, pad_r)), (0, max(0, pad_c))),
                     mode="edge")
    return arr


def to_tissue_grid(arr, level0_shape, stride):
    """`arr` (a whole-slide array at any resolution >= the grid's) on the grid.

    The rows and columns kept are the ones `read_region_lowres(..., ds)`
    keeps: level-0 rows `0, ds, 2*ds, ...`, each mapped into `arr` by the
    actual per-axis shape ratio (floor). Returns `arr` itself when it is
    already the grid, and None when it is coarser than the grid (the caller
    then treats the channel as not arrived).
    """
    if arr is None:
        return None
    a = np.asarray(arr)
    if a.ndim == 3 and a.shape[2] == 1:
        a = a[:, :, 0]
    shape = tissue_grid(level0_shape, stride)
    if a.ndim != 2 or a.size == 0:
        return None
    if tuple(a.shape) == tuple(shape):
        return a
    if a.shape[0] < shape[0] or a.shape[1] < shape[1]:
        return None
    h0, w0 = (int(v) for v in level0_shape[:2])
    stride = max(1, int(stride))
    rows0 = np.arange(0, h0, stride)
    cols0 = np.arange(0, w0, stride)
    rows = np.clip((rows0 / (h0 / float(a.shape[0]))).astype(np.int64),
                   0, a.shape[0] - 1)
    cols = np.clip((cols0 / (w0 / float(a.shape[1]))).astype(np.int64),
                   0, a.shape[1] - 1)
    return a[np.ix_(rows, cols)]


class Cancelled(Exception):
    """A saved-stage read was retired (another Save, a close)."""


def _weighted_merge(values, valid, k, bbox, ps, rows, cols):
    """Merge k x k plane blocks into grid blocks, each plane block weighted
    by the LEVEL-0 samples it stands for (the region `bbox` cut at the
    plane's own block edges) -- so a partial edge block counts for what it
    covers, not as a whole one."""
    by0, by1, bx0, bx1 = (int(v) for v in bbox)
    ny, nx = values.shape
    r = np.arange(ny)
    c = np.arange(nx)
    wy = np.clip(np.minimum((r + 1) * ps, by1) - np.maximum(r * ps, by0), 0, None)
    wx = np.clip(np.minimum((c + 1) * ps, bx1) - np.maximum(c * ps, bx0), 0, None)
    weight = np.where(valid, wy[:, None].astype(np.float64) * wx[None, :], 0.0)
    hh, ww = -(-ny // k), -(-nx // k)
    s_ = np.zeros((hh * k, ww * k)); n_ = np.zeros_like(s_)
    s_[:ny, :nx] = values * weight
    n_[:ny, :nx] = weight
    sums = s_.reshape(hh, k, ww, k).sum(axis=(1, 3))
    counts = n_.reshape(hh, k, ww, k).sum(axis=(1, 3))
    out = np.where(counts > 0, sums / np.maximum(counts, 1e-12), 0.0)
    return _fit(out.astype(np.float32), (rows, cols))


#: grid rows reduced per step of a runtime reduction, between cancel checks
REDUCE_BAND_ROWS = 16


def corrected_overview(table, channel, level0_shape, stride, cancelled=None):
    """`channel`'s SAVED corrected pixels on the Tissue Preview grid.

    None when the table does not serve this channel as corrected (missing
    product, unreadable store): there is then no corrected picture to show,
    and the raw slide is not one. Outside the product's region the values
    are 0. `cancelled()` is asked between bands of a runtime reduction; a
    True answer raises `Cancelled`.
    """
    if table.source_of(channel) != SOURCE_CORRECTED:
        return None
    stride = max(1, int(stride))
    rows, cols = tissue_grid(level0_shape, stride)
    region = table.region(channel)
    # A persisted plane at a stride that divides the grid's: slice it and
    # merge k x k of its blocks. Both grids are anchored at the level-0
    # origin, so plane block (i, j) lies inside grid block (i // k, j // k).
    ps = stride
    while ps >= 2:
        if stride % ps == 0:
            plane = table.coarse_plane(channel, ps)
            if plane is not None:
                k = stride // ps
                values, valid = plane.tile((0, rows * k, 0, cols * k))
                if k == 1:
                    return np.where(valid, values, 0.0).astype(np.float32)
                return _weighted_merge(values, valid, k, region.bbox, ps,
                                       rows, cols)
        ps //= 2
    out = np.zeros((rows, cols), np.float32)
    for r0 in range(0, rows, REDUCE_BAND_ROWS):
        if cancelled is not None and cancelled():
            raise Cancelled(channel)
        r1 = min(rows, r0 + REDUCE_BAND_ROWS)
        mean, valid = reduce_corrected(
            region, (r0 * stride, r1 * stride, 0, cols * stride), stride)
        band = np.where(valid, mean, 0.0).astype(np.float32)
        out[r0:r1] = band[:r1 - r0, :cols]
    return out
