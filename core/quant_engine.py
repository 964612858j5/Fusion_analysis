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
)
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

    @classmethod
    def empty(cls, n):
        big = np.iinfo(np.int64).max
        bb = np.empty((n, 4), np.int64)
        bb[:, 0::2] = big
        bb[:, 1::2] = -1
        z = [np.zeros(n, np.int64) for _ in range(6)]
        return cls(cnt=z[0], sx=z[1], sy=z[2], sxx=z[3], syy=z[4], sxy=z[5], bb=bb,
                   bnd=np.zeros(n, np.int64))


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
    def kernel(halo, nucflag, has_nuc, img, cidx, geom, y0, x0,
               cnt, sx, sy, sxx, syy, sxy, bb, bnd,
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

    def __init__(self, geometry, acc, threads=None):
        self.geo = geometry
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
               g.cnt, g.sx, g.sy, g.sxx, g.syy, g.sxy, g.bb, g.bnd,
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
                    if not put(("skip", ti, len(batches))):
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
             should_stop: Optional[Callable[[], bool]] = None):
    """The whole job: (QuantResult, timings). `regions`: chosen expression
    regions (the primary one is always there); `features`: "morphology",
    "nuclear_summary"; the expression layers go to a FeatureMatrixSink at
    `sink_path` (a temporary directory by default)."""
    import tempfile
    settings = settings or QuantSettings()
    stats = normalize_statistics(statistics)
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
        backend = NumbaBackend(geo, acc, settings.compute_threads)
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
    timing["tiles"] = len(tiles)
    timing["channel_groups"] = len(groups)
    timing["accumulator_bytes_per_group"] = bytes_per_channel(n, stats, regs) * \
        max(len(g) for g in groups)
    res = QuantResult(ids=ids.astype(np.uint32),
                      id_column="nucleus_id" if regs[0] == "nucleus" else "cell_id",
                      obs=obs, sink=sink, layers=layer_names,
                      channel_names=[c.name for c in channels], regions=regs, stats=stats,
                      max_label_id=int(job.n_objects), empty_label_ids=empty.astype(np.uint32),
                      nucleus_outside=outside, channel_groups=len(groups))
    return res, timing
