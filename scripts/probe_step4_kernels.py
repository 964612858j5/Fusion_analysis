"""Block S4-1P: which kernel should Step4's fused quantification use?

A probe, not part of the product (nothing imports it). Read-only on the
project; writes only under `--output-dir`.

The contract every contestant computes, in one pass over a tile of cell
labels and a batch of channels (label 0 = background):

    per cell       count, sum x, sum y, sum x^2, sum y^2, sum xy, bounding box
    per cell x ch  sum, sum of squares, min, max

and the finalizer turns them into area, centroid, major / minor axis,
eccentricity (today's formulas) and mean / sum / std / min / max.

Contestants:
    scipy   today's Step4 calls (`scipy.ndimage` per statistic), the reference
    numba   `@njit(parallel=True)`, one private accumulator set per thread
    numba_ch  the same, parallel over channels instead of rows: one shared
            accumulator set, memory independent of the thread count
    rust    `scripts/probe_step4_rust` (Rayon), the same structure, via ctypes
    cupy64  CuPy RawKernel: a thread per row segment, run-length local sums
            in registers, flushed with float64 atomics
    cupy32  CuPy RawKernel: a thread per pixel, float32 atomics (the naive one)

Modes:
    chunks  real tiles of 2048^2 / 4096^2 / 8192^2, 1 or 4 channels, 1 / 8 / 16
            threads; every contestant checked against the reference on the
            tile; chunked reads of the three sources timed as the I/O floor
    full    one contestant over the whole region, tile by tile, all channels
            in batches, compared per cell with S4-0's reference B
            (`fast_corrected/cell_features.csv`)

Sources follow the strict rule of S4-1: a channel Step0 decided `original` is
read raw from the slide; `tophat` / `cucim` MUST come from Step0's
`corrected_channels.zarr`, else the probe stops.
"""

import argparse
import ctypes
import datetime
import json
import os
import resource
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUST_LIB = os.path.join(HERE, "probe_step4_rust", "target", "release", "libprobe_step4_rust.so")
DEFAULT_WS = "~/fusion_data/test1/rois/full_wsi_20260927_121444_6bad"
DEFAULT_RUN = "seg_20260927_124316_stardist_nuclei_expansion"
DEFAULT_REF = "~/fusionflux/bench_step4/test1_tophat/2026-09-27_bdbd29e/fast_corrected/fast_corrected/cell_features.csv"
CHUNK_CHANNELS = ("DAPI", "CD8", "CD3D", "HsBAg")      # 2 raw, 2 tophat (Step0's product)


# ── measuring ────────────────────────────────────────────────────────────

class RssSampler(threading.Thread):
    def __init__(self, period=0.2):
        super().__init__(daemon=True)
        import psutil
        self.proc = psutil.Process()
        self.period = period
        self.peak = 0
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            self.peak = max(self.peak, self.proc.memory_info().rss)
            time.sleep(self.period)


def _sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _gb(nbytes):
    return round(nbytes / 1e9, 3)


# ── the data (strict sources) ────────────────────────────────────────────

class Sources:
    """The cell labels and every channel, read by region, from where S4-1's
    resolver will read them."""

    def __init__(self, workspace, run_id):
        import tifffile
        import zarr
        self.workspace = os.path.realpath(os.path.expanduser(workspace))
        self.run_dir = os.path.join(self.workspace, "step2", "segmentation_runs", run_id)
        meta = json.load(open(os.path.join(self.run_dir, "segmentation_meta.json")))
        store = meta["label_store"]
        self.roi = next(iter(store))
        cell = store[self.roi]["cell"]
        self.mask_path = cell["path"]
        self.n_cells = int(cell["n_objects"])
        self.mask = zarr.open(self.mask_path, mode="r")
        manifest = json.load(open(os.path.join(self.workspace, "roi_manifest.json")))
        self.slide = manifest["source_ome"]
        corrected_path = os.path.join(self.workspace, "step0", "corrected_channels.zarr")
        self.corrected = zarr.open(corrected_path, mode="r") if os.path.exists(corrected_path) else None
        attrs = dict(self.corrected.attrs) if self.corrected is not None else {}
        self.decisions = dict((attrs.get("correction_config") or {}).get("channel_decisions") or {})
        self.corrected_path = corrected_path
        self._tif = tifffile.TiffFile(self.slide)
        self.channels = self._channel_names()
        z = zarr.open(self._tif.aszarr(), mode="r")
        self._raw = z["0"] if hasattr(z, "keys") else z
        self.shape = tuple(self.mask.shape)
        for name in self.channels:                      # strict: fail now, not half-way
            self.source_of(name)

    def _channel_names(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(self._tif.ome_metadata)
        ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
        return [c.get("Name", f"ch_{i:02d}") for i, c in enumerate(root.findall(".//ome:Channel", ns))]

    def source_of(self, name):
        decision = str(self.decisions.get(name, "original")).strip().lower()
        if decision in ("tophat", "cucim"):
            group = self.corrected[self.roi.replace(" ", "_")] if self.corrected is not None else None
            if group is None or name not in group:
                raise SystemExit(f"channel {name}: Step0 decided {decision} but its corrected "
                                 f"product is missing from {self.corrected_path} -- no fallback")
            arr = group[name]
            if tuple(arr.shape) != self.shape:
                raise SystemExit(f"channel {name}: corrected product {arr.shape} != mask {self.shape}")
            return ("corrected", arr)
        return ("raw", self.channels.index(name))

    def read_mask(self, y0, y1, x0, x1):
        return np.ascontiguousarray(self.mask[y0:y1, x0:x1], dtype=np.uint32)

    def read_channel(self, name, y0, y1, x0, x1):
        kind, where = self.source_of(name)
        if kind == "corrected":
            return np.asarray(where[y0:y1, x0:x1], dtype=np.float32)
        return np.asarray(self._raw[where, y0:y1, x0:x1]).astype(np.float32)

    def read_batch(self, names, y0, y1, x0, x1, threads=4):
        out = np.empty((len(names), y1 - y0, x1 - x0), np.float32)

        def one(i):
            out[i] = self.read_channel(names[i], y0, y1, x0, x1)
        if threads <= 1:
            for i in range(len(names)):
                one(i)
        else:
            with ThreadPoolExecutor(threads) as pool:
                list(pool.map(one, range(len(names))))
        return out


# ── the accumulator contract ─────────────────────────────────────────────

class Merged:
    """The reduced accumulators: n = max label + 1 rows, c channels."""

    def __init__(self, n, c):
        self.cnt = np.zeros(n, np.int64)
        self.sx, self.sy, self.sxx, self.syy, self.sxy = (np.zeros(n) for _ in range(5))
        self.bb = np.zeros((n, 4), np.int32)
        self.s = np.zeros((n, c))
        self.ss = np.zeros((n, c))
        self.mn = np.full((n, c), np.inf, np.float32)
        self.mx = np.full((n, c), -np.inf, np.float32)


def finalize(m):
    """Today's formulas (`feature_extract_worker.py:199-231`), in float64."""
    n = np.maximum(m.cnt, 1).astype(np.float64)
    cx, cy = m.sx / n, m.sy / n
    mu20 = m.sxx / n - cx * cx
    mu02 = m.syy / n - cy * cy
    mu11 = m.sxy / n - cx * cy
    tmp = np.sqrt(np.maximum(0.0, (mu20 - mu02) ** 2 + 4.0 * mu11 ** 2))
    lam1 = 0.5 * (mu20 + mu02 + tmp)
    lam2 = np.maximum(0.5 * (mu20 + mu02 - tmp), 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        ecc = np.where(lam1 > 0, np.sqrt(np.maximum(0.0, 1.0 - lam2 / lam1)), 0.0)
    mean = m.s / n[:, None]
    std = np.sqrt(np.maximum(m.ss / n[:, None] - mean * mean, 0.0))
    return {"area": m.cnt, "centroid_y": cy, "centroid_x": cx,
            "major_axis": 4.0 * np.sqrt(lam1), "minor_axis": 4.0 * np.sqrt(lam2), "eccentricity": ecc,
            "mean": mean, "sum": m.s, "std": std, "min": m.mn.astype(np.float64),
            "max": m.mx.astype(np.float64), "bbox": m.bb}


def acc_bytes(threads, n, c):
    """Private accumulators of the CPU contestants."""
    return threads * n * (8 * 6 + 16 + c * (8 + 8 + 4 + 4))


# ── numba ────────────────────────────────────────────────────────────────

_NB = {}


def _numba():
    if _NB:
        return _NB
    import numba
    from numba import njit, prange

    @njit(parallel=True, cache=False)
    def add_tile(labels, img, y0, x0, c0, geom, cnt, sx, sy, sxx, syy, sxy, bb, s, ss, mn, mx):
        T = cnt.shape[0]
        h, w = labels.shape
        cb = img.shape[0]
        n = cnt.shape[1]
        for t in prange(T):
            r0 = t * h // T
            r1 = (t + 1) * h // T
            for r in range(r0, r1):
                iy = y0 + r
                gy = float(iy)
                for q in range(w):
                    lab = labels[r, q]
                    if lab == 0 or lab >= n:
                        continue
                    if geom:
                        ix = x0 + q
                        gx = float(ix)
                        cnt[t, lab] += 1
                        sx[t, lab] += gx
                        sy[t, lab] += gy
                        sxx[t, lab] += gx * gx
                        syy[t, lab] += gy * gy
                        sxy[t, lab] += gx * gy
                        if iy < bb[t, lab, 0]:
                            bb[t, lab, 0] = iy
                        if iy > bb[t, lab, 1]:
                            bb[t, lab, 1] = iy
                        if ix < bb[t, lab, 2]:
                            bb[t, lab, 2] = ix
                        if ix > bb[t, lab, 3]:
                            bb[t, lab, 3] = ix
                    for k in range(cb):
                        v = img[k, r, q]
                        vd = np.float64(v)
                        s[t, lab, c0 + k] += vd
                        ss[t, lab, c0 + k] += vd * vd
                        if v < mn[t, lab, c0 + k]:
                            mn[t, lab, c0 + k] = v
                        if v > mx[t, lab, c0 + k]:
                            mx[t, lab, c0 + k] = v

    @njit(parallel=True, cache=False)
    def merge(cnt, sx, sy, sxx, syy, sxy, bb, s, ss, mn, mx,
              o_cnt, o_sx, o_sy, o_sxx, o_syy, o_sxy, o_bb, o_s, o_ss, o_mn, o_mx):
        T, n = cnt.shape
        c = s.shape[2]
        for lab in prange(n):
            a = 0
            b1 = 0.0
            b2 = 0.0
            b3 = 0.0
            b4 = 0.0
            b5 = 0.0
            y_lo = bb[0, lab, 0]
            y_hi = bb[0, lab, 1]
            x_lo = bb[0, lab, 2]
            x_hi = bb[0, lab, 3]
            for t in range(T):
                a += cnt[t, lab]
                b1 += sx[t, lab]
                b2 += sy[t, lab]
                b3 += sxx[t, lab]
                b4 += syy[t, lab]
                b5 += sxy[t, lab]
                y_lo = min(y_lo, bb[t, lab, 0])
                y_hi = max(y_hi, bb[t, lab, 1])
                x_lo = min(x_lo, bb[t, lab, 2])
                x_hi = max(x_hi, bb[t, lab, 3])
            o_cnt[lab] = a
            o_sx[lab] = b1
            o_sy[lab] = b2
            o_sxx[lab] = b3
            o_syy[lab] = b4
            o_sxy[lab] = b5
            o_bb[lab, 0] = y_lo
            o_bb[lab, 1] = y_hi
            o_bb[lab, 2] = x_lo
            o_bb[lab, 3] = x_hi
            for k in range(c):
                u = 0.0
                v = 0.0
                lo = mn[0, lab, k]
                hi = mx[0, lab, k]
                for t in range(T):
                    u += s[t, lab, k]
                    v += ss[t, lab, k]
                    lo = min(lo, mn[t, lab, k])
                    hi = max(hi, mx[t, lab, k])
                o_s[lab, k] = u
                o_ss[lab, k] = v
                o_mn[lab, k] = lo
                o_mx[lab, k] = hi

    @njit(parallel=True, cache=False)
    def add_tile_ch(labels, img, y0, x0, c0, geom, cnt, sx, sy, sxx, syy, sxy, bb, s, ss, mn, mx):
        """One job per channel (plus one for the geometry), ONE shared
        accumulator set laid out channel-major: memory does not grow with
        the thread count."""
        h, w = labels.shape
        cb = img.shape[0]
        n = cnt.shape[0]
        for j in prange(cb + 1):
            if j == cb:
                if geom:
                    for r in range(h):
                        iy = y0 + r
                        gy = float(iy)
                        for q in range(w):
                            lab = labels[r, q]
                            if lab == 0 or lab >= n:
                                continue
                            ix = x0 + q
                            gx = float(ix)
                            cnt[lab] += 1
                            sx[lab] += gx
                            sy[lab] += gy
                            sxx[lab] += gx * gx
                            syy[lab] += gy * gy
                            sxy[lab] += gx * gy
                            if iy < bb[lab, 0]:
                                bb[lab, 0] = iy
                            if iy > bb[lab, 1]:
                                bb[lab, 1] = iy
                            if ix < bb[lab, 2]:
                                bb[lab, 2] = ix
                            if ix > bb[lab, 3]:
                                bb[lab, 3] = ix
            else:
                c = c0 + j
                for r in range(h):
                    for q in range(w):
                        lab = labels[r, q]
                        if lab == 0 or lab >= n:
                            continue
                        v = img[j, r, q]
                        vd = np.float64(v)
                        s[c, lab] += vd
                        ss[c, lab] += vd * vd
                        if v < mn[c, lab]:
                            mn[c, lab] = v
                        if v > mx[c, lab]:
                            mx[c, lab] = v

    _NB.update(numba=numba, add_tile=add_tile, merge=merge, add_tile_ch=add_tile_ch)
    return _NB


class NumbaAcc:
    name = "numba"

    def __init__(self, threads, n, c):
        nb = _numba()
        nb["numba"].set_num_threads(threads)
        self.T, self.n, self.c = threads, n, c
        self.cnt = np.zeros((threads, n), np.int64)
        self.g = [np.zeros((threads, n)) for _ in range(5)]
        self.bb = np.empty((threads, n, 4), np.int32)
        self.bb[..., 0::2] = np.iinfo(np.int32).max
        self.bb[..., 1::2] = np.iinfo(np.int32).min
        self.s = np.zeros((threads, n, c))
        self.ss = np.zeros((threads, n, c))
        self.mn = np.full((threads, n, c), np.inf, np.float32)
        self.mx = np.full((threads, n, c), -np.inf, np.float32)

    def add_tile(self, labels, img, y0, x0, c0, geom):
        nb = _numba()
        nb["numba"].set_num_threads(self.T)
        nb["add_tile"](labels, img, np.int64(y0), np.int64(x0), np.int64(c0), bool(geom),
                       self.cnt, *self.g, self.bb, self.s, self.ss, self.mn, self.mx)

    def merge(self):
        nb = _numba()
        nb["numba"].set_num_threads(self.T)
        m = Merged(self.n, self.c)
        nb["merge"](self.cnt, *self.g, self.bb, self.s, self.ss, self.mn, self.mx,
                    m.cnt, m.sx, m.sy, m.sxx, m.syy, m.sxy, m.bb, m.s, m.ss, m.mn, m.mx)
        return m

    def close(self):
        pass


# ── rust ─────────────────────────────────────────────────────────────────

_RS = {}


def _rust():
    if _RS:
        return _RS["lib"]
    if not os.path.exists(RUST_LIB):
        raise SystemExit(f"build the Rust contestant first: cargo build --release in "
                         f"{os.path.dirname(os.path.dirname(os.path.dirname(RUST_LIB)))}")
    lib = ctypes.CDLL(RUST_LIB)
    vp, sz, i64, i32 = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int64, ctypes.c_int32
    lib.acc_new.restype = vp
    lib.acc_new.argtypes = [sz, sz, sz]
    lib.acc_free.argtypes = [vp]
    lib.acc_add_tile.argtypes = [vp, vp, sz, sz, vp, sz, i64, i64, sz, i32]
    lib.acc_merge.argtypes = [vp] * 12
    _RS["lib"] = lib
    return lib


def _ptr(a):
    return ctypes.c_void_p(a.ctypes.data)


class RustAcc:
    name = "rust"

    def __init__(self, threads, n, c):
        self.lib = _rust()
        self.n, self.c = n, c
        self.h = self.lib.acc_new(threads, n, c)

    def add_tile(self, labels, img, y0, x0, c0, geom):
        assert labels.flags.c_contiguous and img.flags.c_contiguous
        assert labels.dtype == np.uint32 and img.dtype == np.float32
        h, w = labels.shape
        self.lib.acc_add_tile(self.h, _ptr(labels), h, w, _ptr(img), img.shape[0],
                              int(y0), int(x0), int(c0), int(bool(geom)))

    def merge(self):
        m = Merged(self.n, self.c)
        self.lib.acc_merge(self.h, *[_ptr(a) for a in (m.cnt, m.sx, m.sy, m.sxx, m.syy, m.sxy,
                                                        m.bb, m.s, m.ss, m.mn, m.mx)])
        return m

    def close(self):
        if self.h:
            self.lib.acc_free(self.h)
            self.h = None


# ── cupy ─────────────────────────────────────────────────────────────────

_CUDA_SRC = r"""
#define CB %(cb)d
__device__ __forceinline__ unsigned int fkey(float f) {
    unsigned int u = __float_as_uint(f);
    return (u & 0x80000000u) ? ~u : (u | 0x80000000u);
}
#define FLUSH()                                                              \
    if (cur != 0u && cur < (unsigned int)n) {                                \
        if (geom) {                                                          \
            atomicAdd(&cnt[cur], (unsigned long long)c_cnt);                 \
            atomicAdd(&sx[cur], c_sx);                                       \
            atomicAdd(&sy[cur], (double)c_cnt * gy);                         \
            atomicAdd(&sxx[cur], c_sxx);                                     \
            atomicAdd(&syy[cur], (double)c_cnt * gy * gy);                   \
            atomicAdd(&sxy[cur], gy * c_sx);                                 \
            atomicMin(&bb[cur * 4 + 0], iy); atomicMax(&bb[cur * 4 + 1], iy); \
            atomicMin(&bb[cur * 4 + 2], c_xlo); atomicMax(&bb[cur * 4 + 3], c_xhi); \
        }                                                                    \
        for (int k = 0; k < CB; ++k) {                                       \
            size_t o = (size_t)cur * ctot + c0 + k;                          \
            atomicAdd(&s[o], cs[k]); atomicAdd(&ss[o], css[k]);              \
            atomicMin(&mn[o], fkey(cmn[k])); atomicMax(&mx[o], fkey(cmx[k])); \
        }                                                                    \
    }

// A thread per row segment: sums for the current label stay in registers
// and are flushed (float64 atomics) when the label changes.
extern "C" __global__ void rl64(const unsigned int* lab, const float* img, int h, int w,
        long long y0, long long x0, int ctot, int c0, int geom, int seg, int n,
        unsigned long long* cnt, double* sx, double* sy, double* sxx, double* syy, double* sxy,
        int* bb, double* s, double* ss, unsigned int* mn, unsigned int* mx) {
    long long nseg = (w + seg - 1) / seg;
    long long tid = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= nseg * h) return;
    int r = (int)(tid / nseg);
    int q0 = (int)(tid %% nseg) * seg;
    int q1 = min(q0 + seg, w);
    size_t plane = (size_t)h * w;
    int iy = (int)(y0 + r);
    double gy = (double)iy;
    unsigned int cur = 0u;
    long long c_cnt = 0; double c_sx = 0.0, c_sxx = 0.0; int c_xlo = 0, c_xhi = 0;
    double cs[CB], css[CB]; float cmn[CB], cmx[CB];
    for (int q = q0; q < q1; ++q) {
        size_t p = (size_t)r * w + q;
        unsigned int l = lab[p];
        if (l != cur) {
            FLUSH();
            cur = l; c_cnt = 0; c_sx = 0.0; c_sxx = 0.0;
            c_xlo = (int)(x0 + q); c_xhi = c_xlo;
            for (int k = 0; k < CB; ++k) { cs[k] = 0.0; css[k] = 0.0; cmn[k] = 3.4e38f; cmx[k] = -3.4e38f; }
        }
        if (l == 0u || l >= (unsigned int)n) continue;
        double gx = (double)(x0 + q);
        c_cnt += 1; c_sx += gx; c_sxx += gx * gx; c_xhi = (int)(x0 + q);
        for (int k = 0; k < CB; ++k) {
            float v = img[k * plane + p];
            double vd = (double)v;
            cs[k] += vd; css[k] += vd * vd;
            cmn[k] = fminf(cmn[k], v); cmx[k] = fmaxf(cmx[k], v);
        }
    }
    FLUSH();
}

// A thread per pixel, float32 sums: the naive kernel.
extern "C" __global__ void px32(const unsigned int* lab, const float* img, int h, int w,
        long long y0, long long x0, int ctot, int c0, int geom, int seg, int n,
        unsigned long long* cnt, double* sx, double* sy, double* sxx, double* syy, double* sxy,
        int* bb, float* s, float* ss, unsigned int* mn, unsigned int* mx) {
    long long p = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    size_t plane = (size_t)h * w;
    if (p >= (long long)plane) return;
    unsigned int l = lab[p];
    if (l == 0u || l >= (unsigned int)n) return;
    int iy = (int)(y0 + p / w), ix = (int)(x0 + p %% w);
    if (geom) {
        double gx = ix, gy = iy;
        atomicAdd(&cnt[l], 1ULL);
        atomicAdd(&sx[l], gx); atomicAdd(&sy[l], gy);
        atomicAdd(&sxx[l], gx * gx); atomicAdd(&syy[l], gy * gy); atomicAdd(&sxy[l], gx * gy);
        atomicMin(&bb[l * 4 + 0], iy); atomicMax(&bb[l * 4 + 1], iy);
        atomicMin(&bb[l * 4 + 2], ix); atomicMax(&bb[l * 4 + 3], ix);
    }
    for (int k = 0; k < CB; ++k) {
        float v = img[k * plane + p];
        size_t o = (size_t)l * ctot + c0 + k;
        atomicAdd(&s[o], v); atomicAdd(&ss[o], v * v);
        atomicMin(&mn[o], fkey(v)); atomicMax(&mx[o], fkey(v));
    }
}
"""


def _cupy():
    """CuPy with the pip wheel's nvrtc loaded (the loader does not find it)."""
    import nvidia.cuda_nvrtc as nvrtc
    lib = os.path.join(list(nvrtc.__path__)[0], "lib", "libnvrtc.so.12")
    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
    import cupy
    return cupy


def _fdecode(keys):
    keys = keys.astype(np.uint32)
    neg = (keys & 0x80000000) == 0
    bits = np.where(neg, ~keys, keys & 0x7FFFFFFF).astype(np.uint32)
    return bits.view(np.float32)


class CupyAcc:
    SEG = 32
    _mods = {}                      # (variant, channels) -> compiled kernel

    def __init__(self, variant, n, c):
        cp = self.cp = _cupy()
        self.variant, self.n, self.c = variant, n, c
        self.name = f"cupy{'64' if variant == 'rl64' else '32'}"
        ft = cp.float64 if variant == "rl64" else cp.float32
        self.cnt = cp.zeros(n, cp.uint64)
        self.g = [cp.zeros(n, cp.float64) for _ in range(5)]
        bb = np.empty((n, 4), np.int32)
        bb[:, 0::2] = np.iinfo(np.int32).max
        bb[:, 1::2] = np.iinfo(np.int32).min
        self.bb = cp.asarray(bb)
        self.s = cp.zeros((n, c), ft)
        self.ss = cp.zeros((n, c), ft)
        self.mn = cp.full((n, c), 0xFFFFFFFF, cp.uint32)
        self.mx = cp.zeros((n, c), cp.uint32)
        self.t_h2d = self.t_kernel = 0.0

    def _kernel(self, cb):
        key = (self.variant, cb)
        if key not in CupyAcc._mods:
            CupyAcc._mods[key] = self.cp.RawModule(code=_CUDA_SRC % {"cb": cb}).get_function(self.variant)
        return CupyAcc._mods[key]

    def add_tile(self, labels, img, y0, x0, c0, geom):
        cp = self.cp
        t0 = time.perf_counter()
        d_lab = cp.asarray(labels)
        d_img = cp.asarray(img)
        cp.cuda.Stream.null.synchronize()
        t1 = time.perf_counter()
        h, w = labels.shape
        cb = img.shape[0]
        if self.variant == "rl64":
            total = ((w + self.SEG - 1) // self.SEG) * h
        else:
            total = h * w
        block = 256
        self._kernel(cb)(((total + block - 1) // block,), (block,),
                         (d_lab, d_img, np.int32(h), np.int32(w), np.int64(y0), np.int64(x0),
                          np.int32(self.c), np.int32(c0), np.int32(1 if geom else 0),
                          np.int32(self.SEG), np.int32(self.n),
                          self.cnt, *self.g, self.bb, self.s, self.ss, self.mn, self.mx))
        cp.cuda.Stream.null.synchronize()
        t2 = time.perf_counter()
        self.t_h2d += t1 - t0
        self.t_kernel += t2 - t1
        del d_lab, d_img

    def merge(self):
        m = Merged(self.n, self.c)
        m.cnt[:] = self.cnt.get().astype(np.int64)
        for dst, src in zip((m.sx, m.sy, m.sxx, m.syy, m.sxy), self.g):
            dst[:] = src.get()
        m.bb[:] = self.bb.get()
        m.s[:] = self.s.get()
        m.ss[:] = self.ss.get()
        mn, mx = self.mn.get(), self.mx.get()
        untouched = mn == 0xFFFFFFFF
        m.mn[:] = np.where(untouched, np.inf, _fdecode(mn))
        m.mx[:] = np.where(mx == 0, -np.inf, _fdecode(mx))
        return m

    def close(self):
        self.cp.get_default_memory_pool().free_all_blocks()


class NumbaChannelAcc:
    """Parallel over channels, not rows: one accumulator set in all."""
    name = "numba_ch"

    def __init__(self, threads, n, c):
        _numba()["numba"].set_num_threads(threads)
        self.T, self.n, self.c = threads, n, c
        self.m = Merged(n, c)
        self.m.bb[:, 0::2] = np.iinfo(np.int32).max
        self.m.bb[:, 1::2] = np.iinfo(np.int32).min
        self.s, self.ss = np.zeros((c, n)), np.zeros((c, n))
        self.mn = np.full((c, n), np.inf, np.float32)
        self.mx = np.full((c, n), -np.inf, np.float32)

    def add_tile(self, labels, img, y0, x0, c0, geom):
        nb = _numba()
        nb["numba"].set_num_threads(self.T)
        m = self.m
        nb["add_tile_ch"](labels, img, np.int64(y0), np.int64(x0), np.int64(c0), bool(geom),
                          m.cnt, m.sx, m.sy, m.sxx, m.syy, m.sxy, m.bb,
                          self.s, self.ss, self.mn, self.mx)

    def merge(self):
        m = self.m
        m.s, m.ss = self.s.T.copy(), self.ss.T.copy()
        m.mn, m.mx = self.mn.T.copy(), self.mx.T.copy()
        return m

    def close(self):
        pass


def make_acc(kind, threads, n, c):
    if kind == "numba_ch":
        return NumbaChannelAcc(threads, n, c)
    if kind == "numba":
        return NumbaAcc(threads, n, c)
    if kind == "rust":
        return RustAcc(threads, n, c)
    if kind == "cupy64":
        return CupyAcc("rl64", n, c)
    if kind == "cupy32":
        return CupyAcc("px32", n, c)
    raise ValueError(kind)


# ── the reference: today's scipy calls on the tile ───────────────────────

def scipy_reference(labels, img, n_cells):
    """Today's Step4 on one tile (`feature_extract_worker.py:191-285`), timed
    per part; plus a float64 geometry reference (today's uses float32 xs / ys)."""
    from scipy import ndimage as ndi
    lab = np.arange(1, n_cells + 1)
    h, w = labels.shape
    t = {}
    t0 = time.perf_counter()
    ys = np.repeat(np.arange(h, dtype=np.float32), w).reshape(h, w)
    xs = np.tile(np.arange(w, dtype=np.float32), h).reshape(h, w)
    ones = (labels > 0).astype(np.float32)
    area = ndi.sum(ones, labels, lab)
    ndi.mean(ys, labels, lab)
    ndi.mean(xs, labels, lab)
    ndi.mean(xs * xs, labels, lab)
    ndi.mean(ys * ys, labels, lab)
    ndi.mean(xs * ys, labels, lab)
    del ys, xs, ones
    t["morphology"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    bin_mask = labels > 0
    eroded = ndi.binary_erosion(bin_mask, structure=np.ones((3, 3), bool))
    ndi.sum((bin_mask & ~eroded).astype(np.float32), labels, lab)
    del bin_mask, eroded
    t["perimeter_old"] = time.perf_counter() - t0
    stats = {k: np.zeros((n_cells + 1, img.shape[0])) for k in ("mean", "sum", "std", "min", "max")}
    per = []
    for k in range(img.shape[0]):
        t0 = time.perf_counter()
        ch = img[k]
        stats["mean"][1:, k] = ndi.mean(ch, labels, lab)
        stats["sum"][1:, k] = ndi.sum(ch, labels, lab)
        stats["std"][1:, k] = ndi.standard_deviation(ch, labels, lab)
        stats["min"][1:, k] = ndi.minimum(ch, labels, lab)
        stats["max"][1:, k] = ndi.maximum(ch, labels, lab)
        per.append(time.perf_counter() - t0)
    t["stats_per_channel"] = per
    # float64 geometry and the bounding boxes
    flat = labels.ravel().astype(np.int64)
    yy, xx = np.divmod(np.arange(flat.size, dtype=np.int64), w)
    yy, xx = yy.astype(np.float64), xx.astype(np.float64)
    nb = n_cells + 1
    geo = {"cnt": np.bincount(flat, minlength=nb)[:nb]}
    for key, wgt in (("sx", xx), ("sy", yy), ("sxx", xx * xx), ("syy", yy * yy), ("sxy", xx * yy)):
        geo[key] = np.bincount(flat, weights=wgt, minlength=nb)[:nb]
    del yy, xx, flat
    bbox = np.zeros((nb, 4), np.int32)
    for i, sl in enumerate(ndi.find_objects(labels, max_label=n_cells), start=1):
        if sl is not None:
            bbox[i] = (sl[0].start, sl[0].stop - 1, sl[1].start, sl[1].stop - 1)
    area_full = np.zeros(nb)
    area_full[1:] = area
    return t, stats, geo, bbox, area_full


def _rel(a, b):
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    return float(np.max(np.abs(a - b) / np.maximum(np.abs(b), 1.0))) if a.size else 0.0


def check(fin, stats, geo, bbox, present, y0, x0):
    """Contestant vs the reference, on the labels present in the tile."""
    p = present
    out = {
        "count_exact": bool(np.array_equal(fin["area"][p], geo["cnt"][p])),
        "bbox_exact": bool(np.array_equal(fin["bbox"][p], bbox[p] + np.array([y0, y0, x0, x0], np.int32))),
        "min_exact": bool(np.array_equal(fin["min"][p], stats["min"][p])),
        "max_exact": bool(np.array_equal(fin["max"][p], stats["max"][p])),
        "sum_rel": _rel(fin["sum"][p], stats["sum"][p]),
        "mean_rel": _rel(fin["mean"][p], stats["mean"][p]),
        "std_rel": _rel(fin["std"][p], stats["std"][p]),
    }
    n = np.maximum(geo["cnt"][p], 1)
    out["centroid_x_abs"] = float(np.max(np.abs(fin["centroid_x"][p] - (geo["sx"][p] / n + x0))))
    out["centroid_y_abs"] = float(np.max(np.abs(fin["centroid_y"][p] - (geo["sy"][p] / n + y0))))
    return out


# ── mode: chunks ─────────────────────────────────────────────────────────

def mode_chunks(args, src, out):
    H, W = src.shape
    n = src.n_cells + 1
    threads = [int(t) for t in args.threads.split(",")]
    kernels = args.kernels.split(",")
    results = []
    for size in [int(s) for s in args.sizes.split(",")]:
        y0, x0 = (H - size) // 2, (W - size) // 2
        y1, x1 = y0 + size, x0 + size
        io = {}
        for attempt in ("first", "second"):
            t0 = time.perf_counter()
            labels = src.read_mask(y0, y1, x0, x1)
            io[f"mask_{attempt}"] = time.perf_counter() - t0
            for name in CHUNK_CHANNELS:
                t0 = time.perf_counter()
                src.read_channel(name, y0, y1, x0, x1)
                io[f"{name}[{src.source_of(name)[0]}]_{attempt}"] = time.perf_counter() - t0
            t0 = time.perf_counter()
            img4 = src.read_batch(list(CHUNK_CHANNELS), y0, y1, x0, x1, threads=4)
            io[f"batch4_threaded_{attempt}"] = time.perf_counter() - t0
        present = np.unique(labels)
        present = present[present > 0]
        print(f"[{size}] tile ({y0},{x0}) {present.size} labels, id range "
              f"{int(present.min())}..{int(present.max())}; io {json.dumps({k: round(v, 3) for k, v in io.items()})}",
              flush=True)
        ref_t, stats, geo, bbox, _ = scipy_reference(labels, img4, src.n_cells)
        print(f"[{size}] scipy: {json.dumps({k: (round(v, 2) if not isinstance(v, list) else [round(x, 2) for x in v]) for k, v in ref_t.items()})}",
              flush=True)
        row = {"size": size, "origin": [y0, x0], "labels_present": int(present.size),
               "label_range": [int(present.min()), int(present.max())],
               "io_seconds": io, "scipy": ref_t, "runs": []}
        for cb in (1, 4):
            img = np.ascontiguousarray(img4[:cb])
            sub = {k: v[:, :cb] for k, v in stats.items()}
            for kind in kernels:
                for T in (threads if kind in ("numba", "rust", "numba_ch") else [1]):
                    times, merges, h2d, kern = [], [], [], []
                    fin = None
                    for rep in range(args.repeats):
                        acc = make_acc(kind, T, n, cb)
                        t0 = time.perf_counter()
                        acc.add_tile(labels, img, y0, x0, 0, True)
                        times.append(time.perf_counter() - t0)
                        t0 = time.perf_counter()
                        merged = acc.merge()
                        merges.append(time.perf_counter() - t0)
                        if kind.startswith("cupy"):
                            h2d.append(acc.t_h2d)
                            kern.append(acc.t_kernel)
                        if rep == 0:
                            fin = finalize(merged)
                        acc.close()
                        del acc, merged
                    ok = check(fin, sub, geo, bbox, present, y0, x0)
                    rec = {"kernel": kind, "channels": cb, "threads": T,
                           "add_tile_s": float(np.median(times)), "merge_s": float(np.median(merges)),
                           "mpix_per_s": size * size / 1e6 / float(np.median(times)),
                           "acc_bytes": (acc_bytes(T, n, cb) if kind in ("numba", "rust")
                                         else acc_bytes(1, n, cb) if kind == "numba_ch" else None),
                           "check": ok}
                    if h2d:
                        rec["h2d_s"] = float(np.median(h2d))
                        rec["kernel_s"] = float(np.median(kern))
                    row["runs"].append(rec)
                    print(f"[{size}] {kind:7s} C={cb} T={T:2d}: tile {rec['add_tile_s']:.3f}s "
                          f"merge {rec['merge_s']:.3f}s "
                          + (f"(h2d {rec['h2d_s']:.3f} kernel {rec['kernel_s']:.3f}) " if h2d else "")
                          + f"{json.dumps(ok)}", flush=True)
        results.append(row)
        del labels, img4, stats, geo, bbox
    return {"chunks": results}


def warm_up(kernels):
    """Compile once outside the timings; report how long it took."""
    out = {}
    lab = np.zeros((64, 64), np.uint32)
    lab[10:20, 10:20] = 1
    img = np.ones((4, 64, 64), np.float32)
    for kind in kernels:
        for cb in (1, 4):
            t0 = time.perf_counter()
            acc = make_acc(kind, 2, 2, cb)
            acc.add_tile(lab, np.ascontiguousarray(img[:cb]), 0, 0, 0, True)
            acc.merge()
            acc.close()
            out[f"{kind}_C{cb}"] = round(time.perf_counter() - t0, 2)
    return out


# ── mode: full ───────────────────────────────────────────────────────────

def mode_full(args, src, out):
    H, W = src.shape
    n = src.n_cells + 1
    names = list(src.channels)
    C = len(names)
    tile, cb = args.tile, args.batch
    kind = args.kernels.split(",")[0]
    T = int(args.threads.split(",")[-1])
    acc = make_acc(kind, T, n, C)
    t_mask = t_read = t_comp = 0.0
    tiles = 0
    t_all = time.perf_counter()
    for y0 in range(0, H, tile):
        for x0 in range(0, W, tile):
            y1, x1 = min(H, y0 + tile), min(W, x0 + tile)
            t0 = time.perf_counter()
            labels = src.read_mask(y0, y1, x0, x1)
            t_mask += time.perf_counter() - t0
            if not labels.any():
                continue
            tiles += 1
            for b in range(0, C, cb):
                t0 = time.perf_counter()
                img = src.read_batch(names[b:b + cb], y0, y1, x0, x1, threads=args.read_threads)
                t_read += time.perf_counter() - t0
                t0 = time.perf_counter()
                acc.add_tile(labels, img, y0, x0, b, b == 0)
                t_comp += time.perf_counter() - t0
            print(f"  tile ({y0},{x0}) read {t_read:.1f}s compute {t_comp:.1f}s", flush=True)
    t0 = time.perf_counter()
    merged = acc.merge()
    t_merge = time.perf_counter() - t0
    total = time.perf_counter() - t_all
    fin = finalize(merged)
    rec = {"kernel": kind, "threads": T, "tile": tile, "batch": cb, "read_threads": args.read_threads,
           "tiles": tiles, "seconds": {"total": total, "read_mask": t_mask, "read_channels": t_read,
                                       "compute": t_comp, "merge": t_merge},
           "acc_bytes": (acc_bytes(T, n, C) if kind in ("numba", "rust")
                         else acc_bytes(1, n, C) if kind == "numba_ch" else None)}
    if kind.startswith("cupy"):
        rec["seconds"]["h2d"] = acc.t_h2d
        rec["seconds"]["kernel"] = acc.t_kernel
        rec["gpu_pool_bytes"] = int(acc.cp.get_default_memory_pool().total_bytes())
    acc.close()
    rec["compare"] = compare_reference(fin, names, os.path.expanduser(args.reference), src)
    np.savez_compressed(os.path.join(out, f"full_{kind}.npz"),
                        **{k: v for k, v in fin.items() if k != "bbox"}, bbox=fin["bbox"])
    return {"full": rec}


def _rel_csv(a, b):
    """|a - b| / |b| (absolute where b == 0), NaN-free cells only."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    d = np.abs(a - b)
    nz = b != 0
    d[nz] /= np.abs(b[nz])
    return d


def compare_reference(fin, names, ref_csv, src):
    """Per cell against S4-0's reference B. The CSV holds 6 significant
    digits (`%g`), so agreement means a relative difference <= 1e-5."""
    import pandas as pd
    ref = pd.read_csv(ref_csv)
    ids = ref["cell_id"].to_numpy().astype(np.int64)
    empty = ref["area"].to_numpy() == 0
    out = {"cells_reference": int(ids.size),
           "cells_empty_in_mask": [int(i) for i in ids[empty]],
           "area_exact": bool(np.array_equal(fin["area"][ids], ref["area"].to_numpy()))}
    keep = ~empty
    k_ids = ids[keep]
    cols = {}
    for col in ("centroid_y", "centroid_x", "major_axis", "minor_axis", "eccentricity"):
        d = _rel_csv(fin[col][k_ids], ref[col].to_numpy()[keep])
        cols[col] = {"max_rel": float(d.max()), "cells_over_1e-5": int((d > 1e-5).sum())}
    for s in ("mean", "sum", "std", "min", "max"):
        worst, bad, where = 0.0, 0, None
        for k, name in enumerate(names):
            col = f"{name.replace('/', '_').replace(' ', '_')}_{s}"
            if col not in ref:
                continue
            d = _rel_csv(fin[s][k_ids, k], ref[col].to_numpy()[keep])
            bad += int((d > 1e-5).sum())
            if d.max() > worst:
                worst, where = float(d.max()), name
        cols[s] = {"max_rel": worst, "channel": where, "cell_channels_over_1e-5": bad}
    out["columns"] = cols
    out["old_moments_check"] = old_moments_check(fin, ref, src)
    return out


def old_moments_check(fin, ref, src, size=4096):
    """Is a shape difference the old code's float32 coordinates? On one tile,
    for the cells wholly inside it, redo today's moments exactly as today does
    (float32 xs / ys / products, `ndimage.mean`) and compare three ways."""
    from scipy import ndimage as ndi
    H, W = src.shape
    y0, x0 = (H - size) // 2, (W - size) // 2
    labels = src.read_mask(y0, y0 + size, x0, x0 + size)
    inside = np.setdiff1d(np.unique(labels), np.unique(np.concatenate(
        [labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    inside = inside[inside > 0]
    h, w = labels.shape
    ys = np.repeat(np.arange(y0, y0 + h, dtype=np.float32), w).reshape(h, w)
    xs = np.tile(np.arange(x0, x0 + w, dtype=np.float32), h).reshape(h, w)
    cy, cx = ndi.mean(ys, labels, inside), ndi.mean(xs, labels, inside)
    m20, m02, m11 = (ndi.mean(xs * xs, labels, inside), ndi.mean(ys * ys, labels, inside),
                     ndi.mean(xs * ys, labels, inside))
    mu20, mu02, mu11 = m20 - cx * cx, m02 - cy * cy, m11 - cx * cy
    tmp = np.sqrt(np.maximum(0.0, (mu20 - mu02) ** 2 + 4.0 * mu11 ** 2))
    lam1, lam2 = 0.5 * (mu20 + mu02 + tmp), np.maximum(0.5 * (mu20 + mu02 - tmp), 0.0)
    old = {"major_axis": 4.0 * np.sqrt(lam1), "minor_axis": 4.0 * np.sqrt(lam2),
           "eccentricity": np.where(lam1 > 0, np.sqrt(np.maximum(0.0, 1.0 - lam2 / lam1)), 0.0)}
    r = ref.set_index("cell_id").loc[inside]
    out = {"tile": [y0, x0, size], "cells": int(inside.size)}
    for col in old:
        csv = r[col].to_numpy()
        out[col] = {"new_vs_csv_max_abs": float(np.max(np.abs(fin[col][inside] - csv))),
                    "old_emulated_vs_csv_max_abs": float(np.max(np.abs(old[col] - csv))),
                    "new_vs_old_emulated_max_abs": float(np.max(np.abs(fin[col][inside] - old[col])))}
    return out


# ── main ─────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["chunks", "full"])
    p.add_argument("--workspace", default=DEFAULT_WS)
    p.add_argument("--run", default=DEFAULT_RUN)
    p.add_argument("--kernels", default="numba,rust,cupy64,cupy32")
    p.add_argument("--threads", default="1,8,16")
    p.add_argument("--sizes", default="2048,4096,8192")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--tile", type=int, default=4096)
    p.add_argument("--batch", type=int, default=4)
    p.add_argument("--read-threads", type=int, default=4)
    p.add_argument("--reference", default=DEFAULT_REF)
    p.add_argument("--output-dir", default=None)
    args = p.parse_args()

    commit = _sh(f"git -C {ROOT} rev-parse --short HEAD") or "nogit"
    out = os.path.expanduser(args.output_dir or os.path.join(
        "~/fusionflux/bench_step4/test1_tophat", f"{datetime.date.today()}_{commit}_probe"))
    os.makedirs(out, exist_ok=True)
    sampler = RssSampler()
    sampler.start()
    src = Sources(args.workspace, args.run)
    report = {"mode": args.mode, "args": vars(args), "commit": commit,
              "started": datetime.datetime.now().isoformat(timespec="seconds"),
              "workspace": src.workspace, "run_dir": src.run_dir, "mask": src.mask_path,
              "n_cells": src.n_cells, "shape": list(src.shape), "slide": src.slide,
              "corrected_product": src.corrected_path,
              "sources": {c: src.source_of(c)[0] for c in src.channels},
              "machine": {"cpu": _sh("lscpu | grep 'Model name' | sed 's/.*: *//'"),
                          "logical_cpus": os.cpu_count(),
                          "mem": _sh("free -g | awk '/Mem/{print $2\" GB\"}'"),
                          "gpu": _sh("nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"),
                          "rustc": _sh("~/.cargo/bin/rustc --version"),
                          "numba": _sh(f"{sys.executable} -c 'import numba;print(numba.__version__)'"),
                          "storage": _sh(f"df -h {src.workspace} | tail -1")}}
    kernels = args.kernels.split(",")
    report["compile_seconds"] = warm_up(kernels)
    print(f"compile: {report['compile_seconds']}", flush=True)
    body = mode_chunks(args, src, out) if args.mode == "chunks" else mode_full(args, src, out)
    report.update(body)
    sampler.stop_flag = True
    report["peak_rss_gb_sampled"] = _gb(sampler.peak)
    report["peak_rss_gb_maxrss"] = _gb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
    tag = args.mode if args.mode == "chunks" else f"full_{kernels[0]}_t{args.threads.split(',')[-1]}_r{args.read_threads}"
    path = os.path.join(out, f"{tag}.json")
    json.dump(report, open(path, "w"), indent=1, default=float)
    print(f"written {path}; peak RSS {report['peak_rss_gb_sampled']} / {report['peak_rss_gb_maxrss']} GB")


if __name__ == "__main__":
    main()
