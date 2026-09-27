"""
block01/core/quant_engine.py — Step4's streaming quantification (block S4-1).

Qt-free. Three boundaries, each replaceable on its own:

  QuantReader    labels + channel batches as arrays (core/quant_sources.py)
  QuantBackend   one pass over them into per-cell accumulators
                 (NumbaBackend is the product; Rust / CUDA are named only)
  QuantFinalizer the science: morphology and statistics from the
                 accumulators, independent of the backend

One pass per (tile, channel batch), float64 accumulation, one global
accumulator set (a channel-parallel kernel: memory does not grow with the
thread count). Raw channels stay in the slide's dtype (uint8 here); Step0's
corrected channels are float32.

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
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

MORPHOLOGY_VERSION = 2
PERIMETER_DEFINITION = "8-neighbor label-aware boundary pixel count"
FAST_STATS = ("mean", "sum", "std", "min", "max")
MORPHOLOGY_COLUMNS = (
    "area", "centroid_y", "centroid_x",
    "bbox_min_y", "bbox_min_x", "bbox_max_y", "bbox_max_x",
    "major_axis", "minor_axis", "eccentricity", "orientation",
    "equivalent_diameter", "aspect_ratio", "extent", "boundary_pixel_count",
)


class QuantStopped(Exception):
    """The user stopped the job."""


class QuantLabelError(Exception):
    """The labels break the LabelStore contract."""


def channel_column(name, stat):
    safe = str(name).replace("/", "_").replace(" ", "_")
    return f"{safe}_{stat}"


def normalize_statistics(statistics):
    wanted = {str(s).strip().lower() for s in (statistics or [])}
    unknown = sorted(wanted - set(FAST_STATS))
    if unknown:
        raise ValueError(f"not a fast statistic: {unknown}")
    stats = [s for s in FAST_STATS if s in wanted]
    if not stats:
        raise ValueError("choose at least one statistic")
    return stats


# ── accumulators ─────────────────────────────────────────────────────────

@dataclasses.dataclass
class Accumulators:
    """n = max label + 1 rows (row 0 unused), c channels. Coordinates are
    REGION pixels; the finalizer adds the region origin."""
    cnt: np.ndarray              # int64 (n,)
    sx: np.ndarray               # int64 (n,) x = column: exact integer moments
    sy: np.ndarray
    sxx: np.ndarray
    syy: np.ndarray
    sxy: np.ndarray
    bb: np.ndarray               # int64 (n, 4): min y, max y, min x, max x
    bnd: np.ndarray              # int64 (n,)
    s: np.ndarray                # float64 (c, n)
    ss: Optional[np.ndarray]     # float64 (c, n) or None
    mn: Optional[np.ndarray]
    mx: Optional[np.ndarray]

    @classmethod
    def empty(cls, n, c, stats):
        big = np.iinfo(np.int64).max
        bb = np.empty((n, 4), np.int64)
        bb[:, 0::2] = big
        bb[:, 1::2] = -1
        z = [np.zeros(n, np.int64) for _ in range(5)]
        return cls(cnt=np.zeros(n, np.int64), sx=z[0], sy=z[1], sxx=z[2], syy=z[3], sxy=z[4],
                   bb=bb,
                   bnd=np.zeros(n, np.int64), s=np.zeros((c, n)),
                   ss=np.zeros((c, n)) if "std" in stats else None,
                   mn=np.full((c, n), np.inf) if "min" in stats else None,
                   mx=np.full((c, n), -np.inf) if "max" in stats else None)

    def nbytes(self):
        return sum(a.nbytes for a in dataclasses.astuple(self) if isinstance(a, np.ndarray))


# ── backends ─────────────────────────────────────────────────────────────

class QuantBackend:
    """accumulate(halo labels, channel block, global channel indices,
    geometry?, tile origin) ... merge() -> Accumulators."""
    name = "abstract"

    def accumulate(self, halo, block, channel_idx, geometry, y0, x0):
        raise NotImplementedError

    def merge(self):
        raise NotImplementedError


_NB = {}


def _numba_kernel():
    if _NB:
        return _NB["kernel"]
    import numba
    from numba import njit, prange

    @njit(parallel=True, nogil=True, cache=True)
    def kernel(halo, img, cidx, geom, y0, x0, cnt, sx, sy, sxx, syy, sxy, bb, bnd,
               s, ss, mn, mx, want_ss, want_mn, want_mx):
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
                        if want_ss:
                            ss[c, lab] += v * v
                        if want_mn:
                            if v < mn[c, lab]:
                                mn[c, lab] = v
                        if want_mx:
                            if v > mx[c, lab]:
                                mx[c, lab] = v

    _NB.update(numba=numba, kernel=kernel)
    return kernel


class NumbaBackend(QuantBackend):
    """The product backend: a channel-parallel fused kernel (one job per
    channel of the batch plus one for the geometry), ONE accumulator set."""
    name = "numba"

    def __init__(self, n, c, stats, threads=None):
        self.acc = Accumulators.empty(n, c, stats)
        self.threads = int(threads or os.cpu_count() or 1)
        self._dummy = np.zeros((1, 1))

    def accumulate(self, halo, block, channel_idx, geometry, y0, x0):
        kernel = _numba_kernel()
        numba = _NB["numba"]
        numba.set_num_threads(max(1, min(self.threads, numba.config.NUMBA_NUM_THREADS,
                                         len(channel_idx) + 1)))
        a = self.acc
        kernel(np.ascontiguousarray(halo), np.ascontiguousarray(block),
               np.asarray(channel_idx, np.int64), bool(geometry), np.int64(y0), np.int64(x0),
               a.cnt, a.sx, a.sy, a.sxx, a.syy, a.sxy, a.bb, a.bnd, a.s,
               a.ss if a.ss is not None else self._dummy,
               a.mn if a.mn is not None else self._dummy,
               a.mx if a.mx is not None else self._dummy,
               a.ss is not None, a.mn is not None, a.mx is not None)

    def merge(self):
        return self.acc


class RustBackend(QuantBackend):
    name = "rust"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("the Rust backend is not implemented (S4-1 ships Numba)")


class CUDABackend(QuantBackend):
    name = "cuda"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("the CUDA backend is not implemented (S4-1 ships Numba)")


# ── the finalizer ────────────────────────────────────────────────────────

@dataclasses.dataclass
class QuantResult:
    cell_ids: np.ndarray             # uint32, the valid labels (count > 0)
    columns: List[str]
    values: np.ndarray               # float64 (cells, columns)
    max_label_id: int
    empty_label_ids: np.ndarray      # labels 1..max_label_id without pixels


class QuantFinalizer:
    """Morphology and statistics from the accumulators. Axis lengths,
    eccentricity and orientation follow skimage's `regionprops` (inertia
    tensor of the central moments, float64)."""

    def finalize(self, acc, n_objects, origin, channel_names, stats):
        oy, ox = origin
        cnt = acc.cnt
        ids = np.nonzero(cnt)[0]
        ids = ids[ids > 0]
        empty = np.nonzero(cnt[1:n_objects + 1] == 0)[0] + 1
        n = cnt[ids].astype(np.float64)
        # Central moments EXACTLY, in Python integers: n^2 var_x = n Sxx - Sx^2
        # (no cancellation error; a circular cell's two variances tie exactly,
        # as in skimage). Integer / integer division is correctly rounded.
        io = cnt[ids].astype(object)
        sx, sy = acc.sx[ids].astype(object), acc.sy[ids].astype(object)
        A = io * acc.sxx[ids].astype(object) - sx * sx
        B = io * acc.syy[ids].astype(object) - sy * sy
        C = io * acc.sxy[ids].astype(object) - sx * sy
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
        bb = acc.bb[ids]
        bbox_area = (bb[:, 1] - bb[:, 0] + 1).astype(np.float64) * (bb[:, 3] - bb[:, 2] + 1)
        morph = {
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
            "boundary_pixel_count": acc.bnd[ids].astype(np.float64),
        }
        columns = ["cell_id"] + list(MORPHOLOGY_COLUMNS)
        arrays = [ids.astype(np.float64)] + [morph[c] for c in MORPHOLOGY_COLUMNS]
        for ci, name in enumerate(channel_names):
            s = acc.s[ci, ids]
            mean = s / n
            for stat in stats:
                if stat == "mean":
                    v = mean
                elif stat == "sum":
                    v = s
                elif stat == "std":
                    v = np.sqrt(np.maximum(acc.ss[ci, ids] / n - mean * mean, 0.0))
                elif stat == "min":
                    v = acc.mn[ci, ids]
                else:
                    v = acc.mx[ci, ids]
                columns.append(channel_column(name, stat))
                arrays.append(v)
        return QuantResult(cell_ids=ids.astype(np.uint32), columns=columns,
                           values=np.column_stack(arrays) if arrays else np.empty((0, 0)),
                           max_label_id=int(n_objects),
                           empty_label_ids=empty.astype(np.uint32))


# ── the driver ───────────────────────────────────────────────────────────

@dataclasses.dataclass
class QuantSettings:
    """Starting values, chosen by measurement -- not a contract."""
    tile: int = 4096
    batch_bytes: int = 256 << 20     # per channel batch
    read_threads: int = 8
    compute_threads: Optional[int] = None
    queue_depth: int = 2


def plan_batches(channels, dtype_of, tile, batch_bytes):
    """[[(position, ChannelSource), ...], ...]: one dtype per batch, each
    batch within `batch_bytes` for a full tile."""
    groups: Dict[str, list] = {}
    for pos, ch in enumerate(channels):
        groups.setdefault(np.dtype(dtype_of(ch)).str, []).append((pos, ch))
    out = []
    for key, items in groups.items():
        per = max(1, int(batch_bytes // (tile * tile * np.dtype(key).itemsize)))
        out += [items[i:i + per] for i in range(0, len(items), per)]
    return out


def quantify(job, reader, statistics, settings=None, backend=None,
             progress: Optional[Callable[[int, int, str], None]] = None,
             should_stop: Optional[Callable[[], bool]] = None):
    """One pass over the region: (QuantResult, timings). Reading the next
    (tile, batch) overlaps the kernel on the current one."""
    settings = settings or QuantSettings()
    stats = normalize_statistics(statistics)
    H, W = job.shape
    channels = list(job.channels)
    n = int(job.n_objects) + 1
    backend = backend or NumbaBackend(n, len(channels), stats, settings.compute_threads)
    T = int(settings.tile)
    batches = plan_batches(channels, reader.channel_dtype, T, settings.batch_bytes)
    tiles = [(y, min(H, y + T), x, min(W, x + T)) for y in range(0, H, T) for x in range(0, W, T)]
    total = len(tiles) * len(batches)
    timing = {"read_labels": 0.0, "read_raw": 0.0, "read_corrected": 0.0,
              "compute": 0.0, "wait_for_read": 0.0}
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
                timing["read_labels"] += time.perf_counter() - t0
                core = halo[1:-1, 1:-1]
                top = int(core.max()) if core.size else 0
                if top > job.n_objects:
                    raise QuantLabelError(f"label {top} exceeds the label store's "
                                          f"{job.n_objects} objects")
                if top == 0:
                    if not put(("skip", ti, len(batches))):
                        return
                    continue
                for bi, batch in enumerate(batches):
                    if halt.is_set():
                        return
                    sources = [ch for _pos, ch in batch]
                    t0 = time.perf_counter()
                    block = reader.channels(sources, y0, y1, x0, x1)
                    key = "read_corrected" if sources[0].kind == "corrected" else "read_raw"
                    timing[key] += time.perf_counter() - t0
                    if not put(("block", ti, halo, block, [p for p, _c in batch], bi == 0)):
                        return
            put((DONE,))
        except BaseException as exc:                            # noqa: BLE001
            put(("error", exc))

    producer = threading.Thread(target=produce, name="step4-producer", daemon=True)
    producer.start()
    done = 0
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
                _kind, ti, halo, block, idx, geom = item
                y0, _y1, x0, _x1 = tiles[ti]
                t0 = time.perf_counter()
                backend.accumulate(halo, block, idx, geom, y0, x0)
                timing["compute"] += time.perf_counter() - t0
                done += 1
            if progress is not None:
                progress(done, total, f"tile {item[1] + 1}/{len(tiles)}")
    finally:
        halt.set()
        producer.join()
    acc = backend.merge()
    result = QuantFinalizer().finalize(acc, job.n_objects, (job.bbox[0], job.bbox[2]),
                                       [c.name for c in channels], stats)
    timing["accumulator_bytes"] = acc.nbytes()
    timing["tiles"] = len(tiles)
    timing["batches_per_tile"] = len(batches)
    return result, timing
