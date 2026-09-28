"""
block01/core/quant_engine.py — Step4's streaming quantification (blocks S4-1, S4-2).

Qt-free. Three boundaries, each replaceable on its own:

  QuantReader    labels + channel batches as arrays (core/quant_sources.py)
  QuantBackend   one pass over them into per-object accumulators
                 (NumbaBackend is the product; Rust / CUDA are named only)
  QuantFinalizer the science: morphology and statistics from the
                 accumulators, independent of the backend

One pass per (tile, channel batch), float64 accumulation, one global
accumulator set (a channel-parallel kernel: memory does not grow with the
thread count). Raw channels stay in the slide's dtype (uint8 here); Step0's
corrected channels are float32.

Expression regions (block S4-2): the primary object (a cell, or a nucleus in
a nuclei-only run) and, for a cell run that kept its nuclei, the nucleus and
the cytoplasm of each cell, defined pixel by pixel:
  nucleus of cell c   = cell == c AND nucleus > 0 AND nucleus_to_cell[nucleus] == c
  cytoplasm of cell c = cell == c AND not in its nucleus
Count / sum / sum of squares of the cytoplasm = cell - nucleus (the same
pixels minus a subset: exact); its min / max are accumulated directly. A
region without pixels gives NaN.

Memory (block S4-2): the channels are split into groups that fit an
accumulator budget; each group is one pass over the tiles, finalised at once
into a `FeatureMatrixSink` (float32 columns on disk) and freed. No step holds
every selected layer in memory at once.

`boundary_pixel_count` (morphology_version 2): a pixel of cell L counts when
any pixel of its 3x3 neighbourhood has a label other than L -- another cell or
background; outside the region counts as background. Tiles carry a one-pixel
ring, so the count equals the whole-region computation. It is NOT the old
union-erosion `perimeter` (which missed the border between touching cells)
and not a Euclidean / Crofton perimeter.
"""

from __future__ import annotations

import dataclasses
import os
import queue
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

MORPHOLOGY_VERSION = 2
PERIMETER_DEFINITION = "8-neighbor label-aware boundary pixel count"
FAST_STATS = ("mean", "sum", "std", "min", "max")
REGIONS = ("cell", "nucleus", "cytoplasm")
MORPHOLOGY_COLUMNS = (
    "area", "centroid_y", "centroid_x",
    "bbox_min_y", "bbox_min_x", "bbox_max_y", "bbox_max_x",
    "major_axis", "minor_axis", "eccentricity", "orientation",
    "equivalent_diameter", "aspect_ratio", "extent", "boundary_pixel_count",
    "perimeter_crofton", "circularity",
)
# block S4-3: skimage's perimeter_crofton(directions=4) weights of the 16
# configurations of a 2 x 2 window (bits: top-left 8, top-right 2,
# bottom-left 4, bottom-right 1; skimage/measure/_regionprops_utils.py)
CROFTON_COEFS = np.array([
    0, np.pi / 4 * (1 + 1 / np.sqrt(2)), np.pi / (4 * np.sqrt(2)), np.pi / (2 * np.sqrt(2)),
    0, np.pi / 4 * (1 + 1 / np.sqrt(2)), 0, np.pi / (4 * np.sqrt(2)),
    np.pi / 4, np.pi / 2, np.pi / (4 * np.sqrt(2)), np.pi / (4 * np.sqrt(2)),
    np.pi / 4, np.pi / 2, 0, 0])
PERIMETER_RECORD = {"perimeter_method": "crofton", "crofton_directions": 4,
                    "perimeter_unit": "pixel",
                    "circularity": "4*pi*area/perimeter_crofton^2 (not clipped; NaN when P = 0)"}
NUCLEAR_SUMMARY_COLUMNS = ("n_nuclei", "nuclear_area", "nuclear_area_mean", "nuclear_area_max",
                           "nuclear_fraction", "cytoplasm_area")
INTEGER_COLUMNS = frozenset({"cell_id", "nucleus_id", "area", "bbox_min_y", "bbox_min_x",
                             "bbox_max_y", "bbox_max_x", "boundary_pixel_count", "n_nuclei",
                             "nuclear_area", "cytoplasm_area"})


class QuantStopped(Exception):
    """The user stopped the job."""


class QuantLabelError(Exception):
    """The labels break the LabelStore contract."""


def channel_column(name, stat, region=None):
    """The CSV column: `<channel>_<stat>` for the primary object (S4-1's
    names), `<channel>_<region>_<stat>` for the nucleus / cytoplasm."""
    safe = str(name).replace("/", "_").replace(" ", "_")
    return f"{safe}_{stat}" if region is None else f"{safe}_{region}_{stat}"


def normalize_statistics(statistics):
    wanted = {str(s).strip().lower() for s in (statistics or [])}
    unknown = sorted(wanted - set(FAST_STATS))
    if unknown:
        raise ValueError(f"not a fast statistic: {unknown}")
    stats = [s for s in FAST_STATS if s in wanted]
    if not stats:
        raise ValueError("choose at least one statistic")
    return stats


def primary_region(job):
    return "nucleus" if getattr(job, "compartment", "cell") == "nucleus" else "cell"


def normalize_regions(regions, job):
    """[primary, then the chosen secondary regions]; nucleus / cytoplasm
    only for a cell run that kept its nuclei."""
    primary = primary_region(job)
    wanted = {str(r).strip().lower() for r in (regions or [])}
    out = [primary]
    if getattr(job, "has_nuclei", False):
        out += [r for r in ("nucleus", "cytoplasm") if r in wanted]
    elif wanted - {primary}:
        raise ValueError("this run has no nuclei beside its cells: "
                         "nucleus / cytoplasm are not available")
    return out


def x_statistic(stats):
    """X = the primary region's first chosen statistic, in FAST_STATS order."""
    return normalize_statistics(stats)[0]


# ── accumulators ─────────────────────────────────────────────────────────

@dataclasses.dataclass
class Geometry:
    """n = max label + 1 rows (row 0 unused). Coordinates are REGION pixels;
    the finalizer adds the region origin."""
    cnt: np.ndarray              # int64 (n,)
    sx: np.ndarray               # int64 (n,) x = column: exact integer moments
    sy: np.ndarray
    sxx: np.ndarray
    syy: np.ndarray
    sxy: np.ndarray
    bb: np.ndarray               # int64 (n, 4): min y, max y, min x, max x
    bnd: np.ndarray              # int64 (n,)
    cr: np.ndarray               # int32 (n, 16): 2 x 2 window configurations (Crofton)

    @classmethod
    def empty(cls, n):
        big = np.iinfo(np.int64).max
        bb = np.empty((n, 4), np.int64)
        bb[:, 0::2] = big
        bb[:, 1::2] = -1
        z = [np.zeros(n, np.int64) for _ in range(6)]
        return cls(cnt=z[0], sx=z[1], sy=z[2], sxx=z[3], syy=z[4], sxy=z[5], bb=bb,
                   bnd=np.zeros(n, np.int64), cr=np.zeros((n, 16), np.int32))


@dataclasses.dataclass
class ChannelAcc:
    """One channel group's accumulators, (group channels, n) each; None when
    the chosen statistics / regions do not need it."""
    s: np.ndarray                # primary object
    ss: Optional[np.ndarray]
    mn: Optional[np.ndarray]
    mx: Optional[np.ndarray]
    ns: Optional[np.ndarray]     # nucleus of the cell (also for the cytoplasm)
    nss: Optional[np.ndarray]
    nmn: Optional[np.ndarray]
    nmx: Optional[np.ndarray]
    ymn: Optional[np.ndarray]    # cytoplasm, accumulated directly
    ymx: Optional[np.ndarray]

    @classmethod
    def empty(cls, n, c, stats, regions):
        std, lo, hi = "std" in stats, "min" in stats, "max" in stats
        nuc, cyt = "nucleus" in regions[1:], "cytoplasm" in regions[1:]

        def z():
            return np.zeros((c, n))

        def f(v):
            return np.full((c, n), v)
        return cls(s=z(), ss=z() if std else None,
                   mn=f(np.inf) if lo else None, mx=f(-np.inf) if hi else None,
                   ns=z() if (nuc or cyt) else None,
                   nss=z() if (std and (nuc or cyt)) else None,
                   nmn=f(np.inf) if (nuc and lo) else None,
                   nmx=f(-np.inf) if (nuc and hi) else None,
                   ymn=f(np.inf) if (cyt and lo) else None,
                   ymx=f(-np.inf) if (cyt and hi) else None)

    def nbytes(self):
        return sum(a.nbytes for a in dataclasses.astuple(self) if isinstance(a, np.ndarray))


def bytes_per_channel(n, stats, regions):
    return ChannelAcc.empty(n, 1, stats, regions).nbytes()


# ── backends ─────────────────────────────────────────────────────────────

class QuantBackend:
    """accumulate(halo labels, nucleus flags, channel block, group channel
    indices, geometry?, tile origin)."""
    name = "abstract"

    def accumulate(self, halo, nucflag, block, channel_idx, geometry, y0, x0):
        raise NotImplementedError


_NB = {}


def _numba_kernel():
    if _NB:
        return _NB["kernel"]
    import numba
    from numba import njit, prange

    @njit(parallel=True, nogil=True, cache=True)
    def kernel(halo, nucflag, has_nuc, img, cidx, geom, y0, x0, ext_r, ext_c,
               cnt, sx, sy, sxx, syy, sxy, bb, bnd, cr,
               s, ss, mn, mx, ns, nss, nmn, nmx, ymn, ymx,
               w_ss, w_mn, w_mx, w_ns, w_nss, w_nmn, w_nmx, w_ymn, w_ymx):
        h = halo.shape[0] - 2
        w = halo.shape[1] - 2
        cb = img.shape[0]
        n = cnt.shape[0]
        for j in prange(cb + 1):
            if j == cb:
                if not geom:
                    continue
                for r in range(h):
                    iy = y0 + r
                    for q in range(w):
                        lab = halo[r + 1, q + 1]
                        if lab == 0 or lab >= n:
                            continue
                        ix = x0 + q
                        cnt[lab] += 1
                        sx[lab] += ix
                        sy[lab] += iy
                        sxx[lab] += ix * ix
                        syy[lab] += iy * iy
                        sxy[lab] += ix * iy
                        if iy < bb[lab, 0]:
                            bb[lab, 0] = iy
                        if iy > bb[lab, 1]:
                            bb[lab, 1] = iy
                        if ix < bb[lab, 2]:
                            bb[lab, 2] = ix
                        if ix > bb[lab, 3]:
                            bb[lab, 3] = ix
                        edge = False
                        for dy in range(3):
                            for dx in range(3):
                                if halo[r + dy, q + dx] != lab:
                                    edge = True
                        if edge:
                            bnd[lab] += 1
                # Crofton: every 2 x 2 window, owned by the tile of its
                # bottom-right pixel; the region's last row / column of
                # windows (bottom-right just outside) by the edge tiles
                for rr in range(1, h + 1 + ext_r):
                    for qq in range(1, w + 1 + ext_c):
                        tl = halo[rr - 1, qq - 1]
                        tr = halo[rr - 1, qq]
                        bl = halo[rr, qq - 1]
                        br = halo[rr, qq]
                        if tl == 0 and tr == 0 and bl == 0 and br == 0:
                            continue
                        for pick in range(4):
                            lab = tl if pick == 0 else (tr if pick == 1 else
                                                        (bl if pick == 2 else br))
                            if lab == 0 or lab >= n:
                                continue
                            if pick >= 1 and lab == tl:
                                continue
                            if pick >= 2 and lab == tr:
                                continue
                            if pick == 3 and lab == bl:
                                continue
                            code = 0
                            if tl == lab:
                                code += 8
                            if tr == lab:
                                code += 2
                            if bl == lab:
                                code += 4
                            if br == lab:
                                code += 1
                            cr[lab, code] += 1
            else:
                c = cidx[j]
                for r in range(h):
                    for q in range(w):
                        lab = halo[r + 1, q + 1]
                        if lab == 0 or lab >= n:
                            continue
                        v = np.float64(img[j, r, q])
                        s[c, lab] += v
                        if w_ss:
                            ss[c, lab] += v * v
                        if w_mn:
                            if v < mn[c, lab]:
                                mn[c, lab] = v
                        if w_mx:
                            if v > mx[c, lab]:
                                mx[c, lab] = v
                        if has_nuc:
                            if nucflag[r, q]:
                                if w_ns:
                                    ns[c, lab] += v
                                if w_nss:
                                    nss[c, lab] += v * v
                                if w_nmn:
                                    if v < nmn[c, lab]:
                                        nmn[c, lab] = v
                                if w_nmx:
                                    if v > nmx[c, lab]:
                                        nmx[c, lab] = v
                            else:
                                if w_ymn:
                                    if v < ymn[c, lab]:
                                        ymn[c, lab] = v
                                if w_ymx:
                                    if v > ymx[c, lab]:
                                        ymx[c, lab] = v

    _NB.update(numba=numba, kernel=kernel)
    return kernel


class NumbaBackend(QuantBackend):
    """The product backend: a channel-parallel fused kernel (one job per
    channel of the batch plus one for the geometry), ONE accumulator set."""
    name = "numba"

    def __init__(self, geometry, acc, threads=None, region_shape=None):
        self.geo = geometry
        self.region_shape = region_shape
        self.acc = acc
        self.threads = int(threads or os.cpu_count() or 1)
        self._d2 = np.zeros((1, 1))
        self._flag = np.zeros((1, 1), np.uint8)

    def accumulate(self, halo, nucflag, block, channel_idx, geometry, y0, x0):
        kernel = _numba_kernel()
        numba = _NB["numba"]
        numba.set_num_threads(max(1, min(self.threads, numba.config.NUMBA_NUM_THREADS,
                                         len(channel_idx) + 1)))
        g, a, d = self.geo, self.acc, self._d2

        def arr(x):
            return x if x is not None else d
        kernel(np.ascontiguousarray(halo),
               np.ascontiguousarray(nucflag) if nucflag is not None else self._flag,
               nucflag is not None, np.ascontiguousarray(block),
               np.asarray(channel_idx, np.int64), bool(geometry), np.int64(y0), np.int64(x0),
               np.int64(1 if (self.region_shape and y0 + halo.shape[0] - 2 == self.region_shape[0])
                        else 0),
               np.int64(1 if (self.region_shape and x0 + halo.shape[1] - 2 == self.region_shape[1])
                        else 0),
               g.cnt, g.sx, g.sy, g.sxx, g.syy, g.sxy, g.bb, g.bnd, g.cr,
               a.s, arr(a.ss), arr(a.mn), arr(a.mx), arr(a.ns), arr(a.nss), arr(a.nmn),
               arr(a.nmx), arr(a.ymn), arr(a.ymx),
               a.ss is not None, a.mn is not None, a.mx is not None, a.ns is not None,
               a.nss is not None, a.nmn is not None, a.nmx is not None, a.ymn is not None,
               a.ymx is not None)


class RustBackend(QuantBackend):
    name = "rust"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("the Rust backend is not implemented (S4-1 ships Numba)")


class CUDABackend(QuantBackend):
    name = "cuda"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("the CUDA backend is not implemented (S4-1 ships Numba)")


# ── the sink ─────────────────────────────────────────────────────────────

class FeatureMatrixSink:
    """float32 layers `<region>_<stat>` of shape (objects, channels) on disk
    (a zarr group), filled a channel group at a time and read back a row
    block at a time -- never all in memory."""

    def __init__(self, path, n_rows, n_channels, layers, chunk_rows=65536, dtype="f4"):
        import zarr
        self.path = path
        self.root = zarr.open_group(path, mode="w")
        self.layers = list(layers)
        self.n_rows = int(n_rows)
        for name in self.layers:
            self.root.create_dataset(name, shape=(self.n_rows, n_channels), dtype=dtype,
                                     chunks=(max(1, min(chunk_rows, self.n_rows)), n_channels),
                                     fill_value=np.nan)

    def write(self, layer, c0, values):
        """values: float64 (objects, group channels) for channels c0 ..."""
        self.root[layer][:, c0:c0 + values.shape[1]] = values

    def add(self, layer, width, dtype="f8", chunk_rows=65536):
        """A further (objects, width) layer (block S4-3: distribution
        statistics of the chosen markers)."""
        self.root.create_dataset(layer, shape=(self.n_rows, width), dtype=dtype,
                                 chunks=(max(1, min(chunk_rows, self.n_rows)), max(1, width)),
                                 fill_value=np.nan)

    def write_rows(self, layer, r0, c0, values):
        self.root[layer][r0:r0 + values.shape[0], c0:c0 + values.shape[1]] = values

    def read(self, layer, r0=0, r1=None):
        return np.asarray(self.root[layer][r0:r1])


# ── the finalizer ────────────────────────────────────────────────────────

class QuantFinalizer:
    """Morphology and statistics from the accumulators. Axis lengths,
    eccentricity and orientation follow skimage's `regionprops` (inertia
    tensor of the central moments, float64)."""

    @staticmethod
    def morphology(geo, ids, origin):
        oy, ox = origin
        cnt = geo.cnt
        n = cnt[ids].astype(np.float64)
        # Central moments EXACTLY, in Python integers: n^2 var_x = n Sxx - Sx^2
        # (no cancellation error; a circular cell's two variances tie exactly,
        # as in skimage). Integer / integer division is correctly rounded.
        io = cnt[ids].astype(object)
        sx, sy = geo.sx[ids].astype(object), geo.sy[ids].astype(object)
        A = io * geo.sxx[ids].astype(object) - sx * sx
        B = io * geo.syy[ids].astype(object) - sy * sy
        C = io * geo.sxy[ids].astype(object) - sx * sy
        n2 = io * io
        cx = (sx / io).astype(np.float64)
        cy = (sy / io).astype(np.float64)
        var_x = (A / n2).astype(np.float64)
        var_y = (B / n2).astype(np.float64)
        cov = (C / n2).astype(np.float64)
        tie = (A == B).astype(bool)
        tmp = np.sqrt(np.maximum(0.0, (var_x - var_y) ** 2 + 4.0 * cov ** 2))
        l1 = np.maximum(0.5 * (var_x + var_y + tmp), 0.0)
        l2 = np.maximum(0.5 * (var_x + var_y - tmp), 0.0)
        major = 4.0 * np.sqrt(l1)
        minor = 4.0 * np.sqrt(l2)
        with np.errstate(invalid="ignore", divide="ignore"):
            ecc = np.where(l1 > 0, np.sqrt(np.maximum(0.0, 1.0 - l2 / l1)), 0.0)
            aspect = np.where(minor > 0, major / np.where(minor > 0, minor, 1.0), np.nan)
        # skimage: a = var_x, b = -cov, c = var_y;
        # a == c -> pi/4 if b < 0 else -pi/4; else 0.5 * atan2(-2b, c - a)
        orient = np.where(tie, np.where(cov > 0, np.pi / 4, -np.pi / 4),
                          0.5 * np.arctan2(2.0 * cov, ((B - A) / n2).astype(np.float64)))
        bb = geo.bb[ids]
        bbox_area = (bb[:, 1] - bb[:, 0] + 1).astype(np.float64) * (bb[:, 3] - bb[:, 2] + 1)
        perim = geo.cr[ids].astype(np.float64) @ CROFTON_COEFS
        with np.errstate(invalid="ignore", divide="ignore"):
            circ = np.where(perim > 0, 4.0 * np.pi * n / np.where(perim > 0, perim, 1.0) ** 2,
                            np.nan)
        return {
            "area": n,
            "centroid_y": cy + oy,
            "centroid_x": cx + ox,
            "bbox_min_y": (bb[:, 0] + oy).astype(np.float64),
            "bbox_min_x": (bb[:, 2] + ox).astype(np.float64),
            "bbox_max_y": (bb[:, 1] + 1 + oy).astype(np.float64),
            "bbox_max_x": (bb[:, 3] + 1 + ox).astype(np.float64),
            "major_axis": major,
            "minor_axis": minor,
            "eccentricity": ecc,
            "orientation": orient,
            "equivalent_diameter": np.sqrt(4.0 * n / np.pi),
            "aspect_ratio": aspect,
            "extent": n / bbox_area,
            "boundary_pixel_count": geo.bnd[ids].astype(np.float64),
            "perimeter_crofton": perim,
            "circularity": circ,
        }

    @staticmethod
    def nuclear_summary(ids, cell_area, nuc_px, table):
        """Per cell, from the nuclei's pixels INSIDE their cell (`nuc_px`,
        indexed by nucleus id) and the nucleus -> cell table."""
        n_cells = int(ids.max()) + 1 if ids.size else 1
        k = np.nonzero(nuc_px[1:] > 0)[0] + 1
        parent = table[k].astype(np.int64)
        px = nuc_px[k].astype(np.float64)
        count = np.bincount(parent, minlength=n_cells).astype(np.float64)
        area = np.bincount(parent, weights=px, minlength=n_cells)
        biggest = np.zeros(n_cells)
        np.maximum.at(biggest, parent, px)
        nn, aa = count[ids], area[ids]
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(nn > 0, aa / np.where(nn > 0, nn, 1), np.nan)
            big = np.where(nn > 0, biggest[ids], np.nan)
        return {"n_nuclei": nn, "nuclear_area": aa, "nuclear_area_mean": mean,
                "nuclear_area_max": big, "nuclear_fraction": aa / cell_area,
                "cytoplasm_area": cell_area - aa}

    @staticmethod
    def expression(acc, ids, cell_n, nic_n, stats, regions):
        """Yields (layer, float64 (objects, group channels)) one layer at a
        time -- the caller writes it to the sink before the next is made."""
        primary = regions[0]
        with np.errstate(invalid="ignore", divide="ignore"):
            for region in regions:
                if region == primary:
                    cnt, s = cell_n, acc.s[:, ids]
                    ss = acc.ss[:, ids] if acc.ss is not None else None
                    lo = acc.mn[:, ids] if acc.mn is not None else None
                    hi = acc.mx[:, ids] if acc.mx is not None else None
                elif region == "nucleus":
                    cnt, s = nic_n, acc.ns[:, ids]
                    ss = acc.nss[:, ids] if acc.nss is not None else None
                    lo = acc.nmn[:, ids] if acc.nmn is not None else None
                    hi = acc.nmx[:, ids] if acc.nmx is not None else None
                else:
                    cnt = cell_n - nic_n
                    s = acc.s[:, ids] - acc.ns[:, ids]
                    ss = (acc.ss[:, ids] - acc.nss[:, ids]) if acc.ss is not None else None
                    lo = acc.ymn[:, ids] if acc.ymn is not None else None
                    hi = acc.ymx[:, ids] if acc.ymx is not None else None
                empty = cnt <= 0
                safe = np.where(empty, 1.0, cnt)
                mean = s / safe
                for stat in stats:
                    if stat == "mean":
                        v = mean
                    elif stat == "sum":
                        v = s.copy()
                    elif stat == "std":
                        v = np.sqrt(np.maximum(ss / safe - mean * mean, 0.0))
                    elif stat == "min":
                        v = lo.copy()
                    else:
                        v = hi.copy()
                    v[:, empty] = np.nan
                    yield f"{region}_{stat}", v.T
                    del v
                del s, ss, lo, hi, mean


# ── the result ───────────────────────────────────────────────────────────

@dataclasses.dataclass
class QuantResult:
    ids: np.ndarray                  # uint32, the objects with pixels, ascending
    id_column: str                   # "cell_id" | "nucleus_id"
    obs: Dict[str, np.ndarray]       # morphology / nuclear summary columns
    sink: FeatureMatrixSink
    layers: List[str]                # "<region>_<stat>", in output order
    channel_names: List[str]
    regions: List[str]
    stats: List[str]
    max_label_id: int
    empty_label_ids: np.ndarray
    nucleus_outside: Dict[str, int]  # nucleus pixels not in their cell
    channel_groups: int
    dist_stats: List[str] = dataclasses.field(default_factory=list)
    dist_markers: List[str] = dataclasses.field(default_factory=list)
    distribution: Optional[Dict] = None   # the pass's record (tile reads, blocks, ...)

    @property
    def dist_layers(self):
        """sink layers "dist_<region>_<stat>" (columns = dist_markers)."""
        return [f"dist_{r}_{st}" for r in self.regions for st in self.dist_stats]

    @property
    def cell_ids(self):
        return self.ids

    @property
    def x_layer(self):
        return f"{self.regions[0]}_{self.stats[0]}"

    def csv_columns(self):
        """(names, sources): the table's columns -- id, obs, then channel by
        channel the primary region's statistics (S4-1's names) and the
        nucleus / cytoplasm ones. A source is ("id"|"obs", name) or
        (layer, channel index)."""
        names, src = [self.id_column], [("id", None)]
        for c in self.obs:
            names.append(c)
            src.append(("obs", c))
        for ci, ch in enumerate(self.channel_names):
            for region in self.regions:
                for stat in self.stats:
                    names.append(channel_column(ch, stat, None if region == self.regions[0]
                                                else region))
                    src.append((f"{region}_{stat}", ci))
        for mi, mk in enumerate(self.dist_markers):
            for region in self.regions:
                for stat in self.dist_stats:
                    names.append(channel_column(mk, stat, None if region == self.regions[0]
                                                else region))
                    src.append((f"dist_{region}_{stat}", mi))
        return names, src

    def block(self, r0, r1):
        """float64 rows r0..r1 of the table in `csv_columns` order."""
        names, src = self.csv_columns()
        cache = {}
        out = np.empty((r1 - r0, len(names)))
        for k, (a, b) in enumerate(src):
            if a == "id":
                out[:, k] = self.ids[r0:r1]
            elif a == "obs":
                out[:, k] = self.obs[b][r0:r1]
            else:
                if a not in cache:
                    cache[a] = self.sink.read(a, r0, r1).astype(np.float64)
                out[:, k] = cache[a][:, b]
        return out

    @property
    def columns(self):
        return self.csv_columns()[0]

    @property
    def values(self):
        """The whole table (small data / tests only)."""
        return self.block(0, self.ids.size)


# ── distribution statistics (block S4-3) ─────────────────────────────────

DIST_STATS = ("median", "p90", "p95", "gini")
DIST_DEFINITIONS = {"median": "numpy.median (mean of the two middle values when even)",
                    "p90": "numpy.percentile(q=90, method='linear')",
                    "p95": "numpy.percentile(q=95, method='linear')",
                    "gini": "2*sum(i*x_i)/(n*sum(x)) - (n+1)/n over the sorted values; "
                            "NaN when sum(x) = 0"}


def normalize_distribution(stats):
    wanted = {str(s).strip().lower() for s in (stats or [])}
    unknown = sorted(wanted - set(DIST_STATS))
    if unknown:
        raise ValueError(f"not a distribution statistic: {unknown}")
    return [s for s in DIST_STATS if s in wanted]


_NBD = {}


def _dist_kernels():
    if _NBD:
        return _NBD
    from numba import njit, prange

    @njit(nogil=True, cache=True)
    def scatter(core, flag, has_flag, img, obj_of, start, nnuc, cur_n, cur_c, bufs):
        """Each pixel of a chosen object to its slot: the object's nuclear
        pixels first, then its cytoplasm (no flag: all in one run). One
        cursor set serves every channel of the batch (same pixel order)."""
        h, w = core.shape
        k = img.shape[0]
        n_lab = obj_of.shape[0]
        for r in range(h):
            for q in range(w):
                lab = core[r, q]
                if lab == 0 or lab >= n_lab:
                    continue
                o = obj_of[lab]
                if o < 0:
                    continue
                if has_flag and flag[r, q]:
                    pos = start[o] + cur_n[o]
                    cur_n[o] += 1
                else:
                    pos = start[o] + nnuc[o] + cur_c[o]
                    cur_c[o] += 1
                for j in range(k):
                    bufs[j, pos] = img[j, r, q]

    @njit(nogil=True, cache=True)
    def lerp(a, b, t):
        # numpy's _lerp (numpy/lib/function_base.py), bit for bit
        d = b - a
        v = a + d * t
        if t >= 0.5:
            v = b - d * (1.0 - t)
        return v

    @njit(nogil=True, cache=True)
    def quantile(x, m, q):
        # numpy.quantile(method="linear"): virtual index (n - 1) * q
        # (numpy/lib/function_base.py, _QuantileMethods["linear"])
        vi = (m - 1) * q
        if vi >= m - 1:
            return x[m - 1]
        if vi < 0:
            return x[0]
        p = np.floor(vi)
        i = np.int64(p)
        return lerp(x[i], x[i + 1], vi - p)

    @njit(nogil=True, cache=True)
    def stats_of(x, m, want, out, row, j, o):
        """x: sorted float64 of length m; want[k]: median, p90, p95, gini."""
        base = row * 4
        if m == 0:
            for k in range(4):
                if want[k]:
                    out[base + k, j, o] = np.nan
            return
        if want[0]:
            if m % 2 == 1:
                out[base, j, o] = x[m // 2]
            else:
                out[base, j, o] = (x[m // 2 - 1] + x[m // 2]) / 2.0
        if want[1]:
            out[base + 1, j, o] = quantile(x, m, 0.9)
        if want[2]:
            out[base + 2, j, o] = quantile(x, m, 0.95)
        if want[3]:
            tot = 0.0
            acc = 0.0
            for i in range(m):
                tot += x[i]
                acc += (i + 1) * x[i]
            out[base + 3, j, o] = np.nan if tot == 0.0 else \
                (2.0 * acc) / (m * tot) - (m + 1.0) / m

    @njit(parallel=True, nogil=True, cache=True)
    def reduce(bufs, start, cnt, nnuc, rows, want, out):
        """Per object and channel: sort its nuclear run and its cytoplasm run
        IN PLACE (no copy of the buffer); the whole cell is the merge of the
        two sorted runs into an object-sized scratch. rows[r] = output row of
        region r (cell, nucleus, cytoplasm) or -1."""
        k = bufs.shape[0]
        n_obj = start.shape[0]
        for o in prange(n_obj):
            s0 = start[o]
            nn = nnuc[o]
            m = cnt[o]
            for j in range(k):
                run1 = bufs[j, s0:s0 + nn]
                run2 = bufs[j, s0 + nn:s0 + m]
                run1.sort()
                run2.sort()
                if rows[1] >= 0:
                    x1 = np.empty(nn)
                    for i in range(nn):
                        x1[i] = run1[i]
                    stats_of(x1, nn, want, out, rows[1], j, o)
                if rows[2] >= 0:
                    x2 = np.empty(m - nn)
                    for i in range(m - nn):
                        x2[i] = run2[i]
                    stats_of(x2, m - nn, want, out, rows[2], j, o)
                if rows[0] >= 0:
                    xm = np.empty(m)
                    a = 0
                    b = 0
                    for i in range(m):
                        if b >= m - nn or (a < nn and run1[a] <= run2[b]):
                            xm[i] = run1[a]
                            a += 1
                        else:
                            xm[i] = run2[b]
                            b += 1
                    stats_of(xm, m, want, out, rows[0], j, o)

    _NBD.update(scatter=scatter, reduce=reduce)
    return _NBD


def _tiles_meeting(tiles, bbox):
    y0, y1, x0, x1 = bbox
    return [i for i, (a, b, c, d) in enumerate(tiles) if a <= y1 and y0 < b and c <= x1 and x0 < d]


def distribution_pass(job, reader, tiles, geo, ids, nic_obj, table, regions, dstats, markers,
                      sink, settings, timing, should_stop=None, progress=None):
    """Distribution statistics of the chosen markers, into sink layers
    "dist_<region>_<stat>". The WORKING SET -- pixel buffers, the label ->
    object map, offsets, cursors and the output block -- stays within
    settings.accumulator_budget: markers are grouped by dtype and count;
    when one marker's buffer alone does not fit, the objects are split into
    blocks (consecutive ids) and each block re-reads only the tiles meeting
    its objects' bounding boxes. Returns the pass's record."""
    kern = _dist_kernels()
    channels = list(job.channels)
    split = bool(table is not None and len(regions) > 1)
    n_obj = int(ids.size)
    n_lab = int(geo.cnt.size)
    cnt_obj = geo.cnt[ids].astype(np.int64)
    nn_obj = nic_obj.astype(np.int64) if split else np.zeros(n_obj, np.int64)
    rows = np.full(3, -1, np.int64)
    for r, name in enumerate(["cell", "nucleus", "cytoplasm"] if regions[0] == "cell"
                             else ["nucleus"]):
        if name in regions:
            rows[r] = regions.index(name)
    want = np.array([s in dstats for s in DIST_STATS], np.bool_)
    n_rows_out = 4 * len(regions)
    for name in [f"dist_{r}_{s}" for r in regions for s in dstats]:
        sink.add(name, len(markers))
    budget = int(settings.accumulator_budget)
    fixed = n_lab * 8                                    # label -> object map

    def per_obj_bytes(k):                                # offsets, cursors, output
        return 4 * 8 + n_rows_out * k * 8

    by_dtype = {}
    for mi, pos in enumerate(markers):
        by_dtype.setdefault(np.dtype(reader.channel_dtype(channels[pos])).str, []).append(mi)
    unique_tiles, total_reads, blocks_run, peak_ws = set(), 0, 0, 0
    t_read = t_compute = 0.0
    csum = np.concatenate([[0], np.cumsum(cnt_obj)])
    for key, mis in by_dtype.items():
        item = np.dtype(key).itemsize
        total_px = int(csum[-1])
        k_fit = (budget - fixed - n_obj * per_obj_bytes(1)) // max(1, total_px * item)
        if k_fit >= 1:
            groups = [mis[i:i + int(k_fit)] for i in range(0, len(mis), int(k_fit))]
            blocks = [(0, n_obj)]
        else:
            groups = [[mi] for mi in mis]
            blocks, b0 = [], 0
            room = budget - fixed
            while b0 < n_obj:
                b1 = b0
                while b1 < n_obj and ((csum[b1 + 1] - csum[b0]) * item
                                      + (b1 + 1 - b0) * per_obj_bytes(1)) <= room:
                    b1 += 1
                if b1 == b0:
                    raise MemoryError("one object alone exceeds the distribution budget")
                blocks.append((b0, b1))
                b0 = b1
        for grp in groups:
            sources = [channels[markers[mi]] for mi in grp]
            for b0, b1 in blocks:
                if should_stop is not None and should_stop():
                    raise QuantStopped()
                nb = b1 - b0
                bpx = int(csum[b1] - csum[b0])
                obj_of = np.full(n_lab, -1, np.int64)
                obj_of[ids[b0:b1].astype(np.int64)] = np.arange(nb)
                start = (csum[b0:b1] - csum[b0]).astype(np.int64)
                cur_n = np.zeros(nb, np.int64)
                cur_c = np.zeros(nb, np.int64)
                bufs = np.empty((len(grp), bpx), np.dtype(key))
                out = np.full((n_rows_out, len(grp), nb), np.nan)
                ws = (obj_of.nbytes + start.nbytes + cur_n.nbytes + cur_c.nbytes + bufs.nbytes
                      + out.nbytes + nn_obj[b0:b1].nbytes)
                peak_ws = max(peak_ws, ws)
                bb = geo.bb[ids[b0:b1].astype(np.int64)]
                box = (int(bb[:, 0].min()), int(bb[:, 1].max()), int(bb[:, 2].min()),
                       int(bb[:, 3].max()))
                t0 = time.perf_counter()
                for ti in _tiles_meeting(tiles, box):
                    y0, y1, x0, x1 = tiles[ti]
                    core = np.ascontiguousarray(reader.labels(y0, y1, x0, x1)[1:-1, 1:-1])
                    flag = np.zeros((1, 1), np.uint8)
                    if split:
                        nuc = reader.nuclei(y0, y1, x0, x1)
                        flag = ((nuc > 0) & (table[nuc] == core)).view(np.uint8)
                    blk = reader.channels(sources, y0, y1, x0, x1)
                    unique_tiles.add(ti)
                    total_reads += 1
                    kern["scatter"](core, flag, split, np.ascontiguousarray(blk), obj_of, start,
                                    nn_obj[b0:b1], cur_n, cur_c, bufs)
                t_read += time.perf_counter() - t0
                if not (np.array_equal(cur_n, nn_obj[b0:b1])
                        and np.array_equal(cur_c, cnt_obj[b0:b1] - nn_obj[b0:b1])):
                    raise QuantLabelError("the distribution pass did not see every pixel "
                                          "of its objects")
                t0 = time.perf_counter()
                kern["reduce"](bufs, start, cnt_obj[b0:b1], nn_obj[b0:b1], rows, want, out)
                for r, region in enumerate(regions):
                    for si, stat in enumerate(DIST_STATS):
                        if stat in dstats:
                            # markers of one dtype need not be adjacent in
                            # the marker list: one column each
                            for jj, mi in enumerate(grp):
                                sink.write_rows(f"dist_{region}_{stat}", b0, mi,
                                                out[r * 4 + si, jj][:, None])
                t_compute += time.perf_counter() - t0
                blocks_run += 1
                del bufs, out, obj_of
                if progress is not None:
                    progress(blocks_run, blocks_run, f"distribution {blocks_run}")
    timing["distribution_read"] = t_read
    timing["distribution_compute"] = t_compute
    return {"markers": [channels[m].name for m in markers], "statistics": list(dstats),
            "regions": list(regions), "definitions": {s: DIST_DEFINITIONS[s] for s in dstats},
            "budget_bytes": budget, "peak_working_set_bytes": int(peak_ws),
            "passes": blocks_run, "unique_tiles_read": len(unique_tiles),
            "total_tile_reads": total_reads,
            "reread_factor": round(total_reads / max(1, len(unique_tiles)), 3),
            "marker_groups": sum(1 for _ in by_dtype)}


# ── the driver ───────────────────────────────────────────────────────────

@dataclasses.dataclass
class QuantSettings:
    """Starting values, chosen by measurement -- not a contract."""
    tile: int = 4096
    batch_bytes: int = 256 << 20     # per channel batch
    read_threads: int = 8
    compute_threads: Optional[int] = None
    queue_depth: int = 2
    accumulator_budget: int = 1536 << 20
    sink_dtype: str = "f4"           # the h5ad's layers; tests check the engine in f8


def plan_batches(channels, dtype_of, tile, batch_bytes):
    """[[(position, ChannelSource), ...], ...]: one dtype per batch, each
    batch within `batch_bytes` for a full tile."""
    groups: Dict[str, list] = {}
    for pos, ch in channels:
        groups.setdefault(np.dtype(dtype_of(ch)).str, []).append((pos, ch))
    out = []
    for key, items in groups.items():
        per = max(1, int(batch_bytes // (tile * tile * np.dtype(key).itemsize)))
        out += [items[i:i + per] for i in range(0, len(items), per)]
    return out


def plan_groups(n_channels, per_channel_bytes, budget):
    per = max(1, int(budget // max(1, per_channel_bytes)))
    return [list(range(i, min(n_channels, i + per))) for i in range(0, n_channels, per)]


def _pass(job, reader, tiles, batches, backend, first, table, counts, timing, settings,
          progress, should_stop, done, total):
    """One pass over the tiles for one channel group. The reader runs ahead
    (a producer thread) while the kernel works on the current batch."""
    has_nuc = table is not None
    q: "queue.Queue" = queue.Queue(maxsize=max(1, int(settings.queue_depth)))
    halt = threading.Event()
    DONE = object()

    def put(item):
        while not halt.is_set():
            try:
                q.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def produce():
        try:
            for ti, (y0, y1, x0, x1) in enumerate(tiles):
                if halt.is_set():
                    return
                t0 = time.perf_counter()
                halo = reader.labels(y0, y1, x0, x1)
                core = halo[1:-1, 1:-1]
                nuc = reader.nuclei(y0, y1, x0, x1) if has_nuc else None
                timing["read_labels"] += time.perf_counter() - t0
                top = int(core.max()) if core.size else 0
                if top > job.n_objects:
                    raise QuantLabelError(f"label {top} exceeds the label store's "
                                          f"{job.n_objects} objects")
                if top == 0:
                    # an empty core can still own 2 x 2 windows (Crofton)
                    # whose top-left pixels are a neighbour's cell: the
                    # geometry job runs on its ring, no channel is read
                    if first and halo.any():
                        if not put(("block", ti, halo, None,
                                    np.zeros((0,) + core.shape, np.uint8), [], True, None)):
                            return
                        if not put(("skip", ti, len(batches) - 1)):
                            return
                    elif not put(("skip", ti, len(batches))):
                        return
                    continue
                flag = None
                if has_nuc:
                    ntop = int(nuc.max()) if nuc.size else 0
                    if ntop >= table.size:
                        raise QuantLabelError(f"nucleus {ntop} exceeds the table's "
                                              f"{table.size - 1} nuclei")
                    flag = ((nuc > 0) & (table[nuc] == core)).view(np.uint8) \
                        if nuc.size else np.zeros(core.shape, np.uint8)
                for bi, batch in enumerate(batches):
                    if halt.is_set():
                        return
                    sources = [ch for _pos, ch in batch]
                    t0 = time.perf_counter()
                    blk = reader.channels(sources, y0, y1, x0, x1)
                    key = "read_corrected" if sources[0].kind == "corrected" else "read_raw"
                    timing[key] += time.perf_counter() - t0
                    extra = (core, nuc, flag) if (first and bi == 0 and has_nuc) else None
                    if not put(("block", ti, halo, flag, blk, [p for p, _c in batch],
                                first and bi == 0, extra)):
                        return
            put((DONE,))
        except BaseException as exc:                            # noqa: BLE001
            put(("error", exc))

    producer = threading.Thread(target=produce, name="step4-producer", daemon=True)
    producer.start()
    try:
        while True:
            if should_stop is not None and should_stop():
                raise QuantStopped()
            t0 = time.perf_counter()
            try:
                item = q.get(timeout=0.1)
            except queue.Empty:
                timing["wait_for_read"] += time.perf_counter() - t0
                continue
            timing["wait_for_read"] += time.perf_counter() - t0
            if item[0] is DONE:
                break
            if item[0] == "error":
                raise item[1]
            if item[0] == "skip":
                done += item[2]
            else:
                _kind, ti, halo, flag, blk, idx, geom, extra = item
                y0, _y1, x0, _x1 = tiles[ti]
                t0 = time.perf_counter()
                backend.accumulate(halo, flag, blk, idx, geom, y0, x0)
                if extra is not None:
                    core, nuc, f = extra
                    inside = f.astype(bool)
                    counts["nic"] += np.bincount(core[inside], minlength=counts["nic"].size)
                    counts["nuc_px"] += np.bincount(nuc[inside], minlength=counts["nuc_px"].size)
                    outside = (nuc > 0) & ~inside
                    counts["out_px"] += np.bincount(nuc[outside],
                                                    minlength=counts["out_px"].size)
                timing["compute"] += time.perf_counter() - t0
                done += 1
            if progress is not None:
                progress(done, total, f"tile {item[1] + 1}/{len(tiles)}")
    finally:
        halt.set()
        producer.join()
    return done


def quantify(job, reader, statistics, regions=None, features=("morphology",), sink_path=None,
             settings=None, progress: Optional[Callable[[int, int, str], None]] = None,
             should_stop: Optional[Callable[[], bool]] = None, distribution=None,
             markers=None):
    """The whole job: (QuantResult, timings). `regions`: chosen expression
    regions (the primary one is always there); `features`: "morphology",
    "nuclear_summary"; the expression layers go to a FeatureMatrixSink at
    `sink_path` (a temporary directory by default)."""
    import tempfile
    settings = settings or QuantSettings()
    stats = normalize_statistics(statistics)
    dstats = normalize_distribution(distribution)
    names = [c.name for c in job.channels]
    unknown = [m for m in (markers or []) if m not in names]
    if unknown:
        raise ValueError(f"not a channel of this slide: {unknown}")
    marker_pos = [names.index(m) for m in names if m in set(markers or [])]
    if dstats and not marker_pos:
        raise ValueError("distribution statistics need at least one marker")
    regs = normalize_regions(regions or [], job)
    features = set(features or ())
    has_nuc = bool(getattr(job, "has_nuclei", False))
    if "nuclear_summary" in features and not has_nuc:
        raise ValueError("this run has no nuclei beside its cells: no nuclear summary")
    need_nuc = has_nuc and (len(regs) > 1 or "nuclear_summary" in features)
    H, W = job.shape
    channels = list(job.channels)
    n = int(job.n_objects) + 1
    T = int(settings.tile)
    tiles = [(y, min(H, y + T), x, min(W, x + T)) for y in range(0, H, T) for x in range(0, W, T)]
    groups = plan_groups(len(channels), bytes_per_channel(n, stats, regs),
                         settings.accumulator_budget)
    table = reader.nucleus_table() if need_nuc else None
    counts = None
    if table is not None:
        m = table.size
        counts = {"nic": np.zeros(n, np.int64), "nuc_px": np.zeros(m, np.int64),
                  "out_px": np.zeros(m, np.int64)}
    timing = {"read_labels": 0.0, "read_raw": 0.0, "read_corrected": 0.0,
              "compute": 0.0, "wait_for_read": 0.0, "finalize": 0.0}
    geo = Geometry.empty(n)
    group_batches = [plan_batches([(p, channels[p]) for p in g], reader.channel_dtype, T,
                                  settings.batch_bytes) for g in groups]
    total = len(tiles) * sum(len(b) for b in group_batches)
    done = 0
    sink = None
    ids = obs = None
    fin = QuantFinalizer()
    layer_names = [f"{r}_{s}" for r in regs for s in stats]
    for gi, (group, batches) in enumerate(zip(groups, group_batches)):
        acc = ChannelAcc.empty(n, len(group), stats, regs)
        backend = NumbaBackend(geo, acc, settings.compute_threads, region_shape=(H, W))
        # the kernel indexes the group's accumulators by position in the group
        local = [[(group.index(p), ch) for p, ch in b] for b in batches]
        done = _pass(job, reader, tiles, local, backend, gi == 0, table, counts, timing,
                     settings, progress, should_stop, done, total)
        t0 = time.perf_counter()
        if gi == 0:
            cnt = geo.cnt
            ids = np.nonzero(cnt)[0]
            ids = ids[ids > 0]
            obs = {}
            cell_n = cnt[ids].astype(np.float64)
            if "morphology" in features:
                obs.update(fin.morphology(geo, ids, (job.bbox[0], job.bbox[2])))
            if "nuclear_summary" in features:
                obs.update(fin.nuclear_summary(ids, cell_n, counts["nuc_px"], table))
            nic_n = counts["nic"][ids].astype(np.float64) if counts is not None \
                else np.zeros(ids.size)
            if sink_path is None:
                sink_path = tempfile.mkdtemp(prefix="step4_sink_")
            sink = FeatureMatrixSink(sink_path, ids.size, len(channels), layer_names,
                                     dtype=settings.sink_dtype)
        for layer, values in fin.expression(acc, ids, cell_n, nic_n, stats, regs):
            sink.write(layer, group[0], values)
            del values
        timing["finalize"] += time.perf_counter() - t0
        del acc, backend
    empty = np.nonzero(geo.cnt[1:n] == 0)[0] + 1
    outside = {"pixels": 0, "nuclei": 0}
    if counts is not None:
        outside = {"pixels": int(counts["out_px"].sum()),
                   "nuclei": int(np.count_nonzero(counts["out_px"][1:]))}
    dist_record = None
    if dstats and marker_pos:
        nic_obj = counts["nic"][ids] if counts is not None else None
        dist_record = distribution_pass(job, reader, tiles, geo, ids, nic_obj, table, regs, dstats,
                                        marker_pos, sink, settings, timing, should_stop)
    timing["tiles"] = len(tiles)
    timing["channel_groups"] = len(groups)
    timing["accumulator_bytes_per_group"] = bytes_per_channel(n, stats, regs) * \
        max(len(g) for g in groups)
    res = QuantResult(ids=ids.astype(np.uint32),
                      id_column="nucleus_id" if regs[0] == "nucleus" else "cell_id",
                      obs=obs, sink=sink, layers=layer_names,
                      channel_names=[c.name for c in channels], regions=regs, stats=stats,
                      max_label_id=int(job.n_objects), empty_label_ids=empty.astype(np.uint32),
                      nucleus_outside=outside, channel_groups=len(groups),
                      dist_stats=dstats if marker_pos else [],
                      dist_markers=[names[p] for p in marker_pos] if dstats else [],
                      distribution=dist_record)
    return res, timing
