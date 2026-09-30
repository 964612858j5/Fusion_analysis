"""
Block A2b-probe of v16 (docs/v16_A2b_probe_application.md v2, plan v2.3.1):
OME-TIFF vs OME-NGFF 0.4, each with a reader adapted to its own layout,
the same resource budget and the same downstream science. A probe: scripts
and scratch copies only; nothing here is imported by the product.

    ingest   --src TIF --out DIR.ome.zarr --chunk 512|1024|2048 --codec lz4|zstd --shuffle byte|bit
    verify   DIR.ome.zarr --src TIF                    generic zarr, independent of our readers
    oracle   --tif TIF --ngff DIR                      every reader bitwise == TIFF
    bench    --tif TIF [--ngff DIR] --side SIDE --workload W --out JSON
    step4    --tif TIF [--ngff DIR] --side SIDE --run RUN --out JSON [--keep DIR]
    fusion   --tif TIF [--ngff DIR] --side SIDE --copy COPY --out JSON [--keep DIR]
    same-step4  DIR_A DIR_B                            scientific payload bitwise
    same-fused  ZARR_A ZARR_B

Sides: tif_product (the product's readers, as they are), tif_adapted (the
same IO classes as NGFF: tile-aligned direct decode, 8 threads, read-ahead 1,
512 MiB block cache), ngff_generic (zarr, A0), ngff_adapted (chunk-aligned
direct Blosc decode, 8 threads, read-ahead 1, 512 MiB block cache).

RESOURCE BUDGET (application v2 §4.2, the same for both formats): 8 read
threads, read-ahead 1, block cache 512 MiB, Blosc's own threads off. The
block cache is cleared at the start of EVERY timed run (cold and warm), so it
only serves reuse within a run; "warm" is the OS page cache, as in A0.
Seeds, workloads, repetitions and cold-cache handling are A0's
(scripts/bench_v16_a0_storage.py): 1 untimed warm-up, 3 cold, 3 warm.
"""

import argparse
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import bench_v16_a0_storage as a0  # noqa: E402

THREADS = 8
PREFETCH = 1
CACHE_BYTES = 512 * 2 ** 20
BUDGET = {"threads": THREADS, "prefetch": PREFETCH, "cache_mib": CACHE_BYTES // 2 ** 20,
          "blosc_threads": "off"}
VIEW_TILE = a0.CHUNK            # the viewer's 512 tile grid (workloads), not a storage chunk
BENCH_WORKLOADS = ["roi512", "roi2048", "roi4096", "viewport_l0", "viewport_l1", "viewport_l2",
                   "channel_switch", "seq29", "step4"]
SIDES = ["tif_product", "tif_adapted", "ngff_generic", "ngff_adapted"]
NEVER_WRITE = os.path.realpath(os.path.expanduser("~/fusion_data"))


def _no_write(path):
    if os.path.realpath(path).startswith(NEVER_WRITE):
        raise SystemExit(f"never write under ~/fusion_data: {path}")


def boxes(y0, y1, x0, x1, ch, cw):
    """(ty, tx, by0, by1, bx0, bx1) of the (ch, cw) blocks covering a region."""
    for ty in range(y0 // ch, (y1 - 1) // ch + 1):
        for tx in range(x0 // cw, (x1 - 1) // cw + 1):
            yield (ty, tx, max(y0, ty * ch), min(y1, (ty + 1) * ch),
                   max(x0, tx * cw), min(x1, (tx + 1) * cw))


class BlockCache:
    """A bounded LRU of decoded blocks (bytes counted)."""

    def __init__(self, limit=CACHE_BYTES):
        self.limit, self.used = int(limit), 0
        self.d = OrderedDict()
        self.lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self.lock:
            v = self.d.get(key)
            if v is None:
                self.misses += 1
                return None
            self.d.move_to_end(key)
            self.hits += 1
            return v

    def put(self, key, arr):
        with self.lock:
            if key in self.d:
                return
            self.d[key] = arr
            self.used += arr.nbytes
            while self.used > self.limit and self.d:
                _, old = self.d.popitem(last=False)
                self.used -= old.nbytes

    def clear(self):
        with self.lock:
            self.d.clear()
            self.used = 0


# ── ingest + independent verification ────────────────────────────────────


def cmd_ingest(args):
    import tifffile
    import zarr
    from numcodecs import Blosc
    _no_write(args.out)
    if os.path.exists(args.out):
        raise SystemExit(f"{args.out} exists")
    meta = a0.tif_meta(args.src)
    attrs, physical = a0.ngff_attrs(meta)
    shuffle = {"byte": Blosc.SHUFFLE, "bit": Blosc.BITSHUFFLE}[args.shuffle]
    comp = Blosc(cname=args.codec, clevel=5, shuffle=shuffle)
    t0 = time.perf_counter()
    root = zarr.group(store=zarr.DirectoryStore(args.out, dimension_separator="/"))
    root.attrs.update(attrs)
    with tifffile.TiffFile(args.src) as tif:
        for k, lv in enumerate(tif.series[0].levels):
            ch = args.chunk if k == 0 else a0.CHUNK
            arr = root.create_dataset(str(k), shape=tuple(lv.shape), chunks=(1, ch, ch),
                                      dtype=lv.dtype, compressor=comp, fill_value=0,
                                      dimension_separator="/")
            for c in range(lv.shape[0]):
                arr[c] = lv.pages[c].asarray(maxworkers=THREADS)
            print(f"[ingest] level {k} {lv.shape} chunk {ch} {time.perf_counter() - t0:.1f}s", flush=True)
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(args.out) for f in fs)
    info = {"src": args.src, "out": args.out, "chunk_l0": args.chunk, "codec": args.codec,
            "shuffle": args.shuffle, "clevel": 5, "physical_units": physical,
            "seconds": round(time.perf_counter() - t0, 1), "bytes": size,
            "source_bytes": os.path.getsize(args.src),
            "ratio_to_source": round(size / os.path.getsize(args.src), 4),
            "peak_rss_mb": round(a0.peak_rss_mb())}
    json.dump(info, open(args.out.rstrip("/") + ".ingest.json", "w"), indent=1)
    print(json.dumps(info))


def cmd_verify(args):
    """NGFF 0.4 structure + pixels through the GENERIC zarr reader only
    (`zarr.open`), independent of the probe's own readers."""
    import tifffile
    import zarr
    meta = a0.tif_meta(args.src)
    info = json.load(open(args.ngff.rstrip("/") + ".ingest.json"))
    root = zarr.open(args.ngff, mode="r")
    fails = []

    def ok(cond, what):
        if not cond:
            fails.append(what)
        print(f"[verify] {'OK  ' if cond else 'FAIL'} {what}")

    ms = root.attrs.get("multiscales")
    ok(isinstance(ms, list) and len(ms) == 1 and ms[0].get("version") == "0.4", "multiscales 0.4")
    ok([a.get("name") for a in ms[0]["axes"]] == ["c", "y", "x"], "axes c, y, x")
    ds = ms[0]["datasets"]
    ok([d["path"] for d in ds] == [str(k) for k in range(len(meta["levels"]))], "dataset paths")
    rng = np.random.default_rng(a0.SEED)
    with tifffile.TiffFile(args.src) as tif:
        for k in range(len(meta["levels"])):
            arr = zarr.open(os.path.join(args.ngff, str(k)), mode="r")        # generic, per array
            want = (1, info["chunk_l0"] if k == 0 else a0.CHUNK) * 1
            ok(list(arr.shape) == meta["levels"][k], f"level {k} shape")
            ok(arr.chunks[1] == (info["chunk_l0"] if k == 0 else a0.CHUNK), f"level {k} chunk {arr.chunks}")
            ok(arr.compressor.cname == info["codec"], f"level {k} codec {arr.compressor}")
            zt = zarr.open(tif.series[0].aszarr(level=k), mode="r")
            c_, h, w = meta["levels"][k]
            bad = 0
            for _ in range(12):
                c = int(rng.integers(0, c_))
                y, x = int(rng.integers(0, max(1, h - 700))), int(rng.integers(0, max(1, w - 700)))
                if not np.array_equal(zt[c, y:y + 700, x:x + 700], arr[c, y:y + 700, x:x + 700]):
                    bad += 1
            ok(bad == 0, f"level {k}: 12 windows via generic zarr bitwise == TIFF")
            del want
    print(f"[verify] {'PASS' if not fails else 'FAIL'}")
    return 0 if not fails else 1


# ── readers ──────────────────────────────────────────────────────────────


class _Reader:
    """Common surface: region / regions / tiles / level_shape / files."""

    pool = None
    cache = None

    def tiles(self, c, k, tiles):
        h, w = self.level_shape(k)

        def one(t):
            ty, tx = t
            return self.region(c, k, ty * VIEW_TILE, min(h, (ty + 1) * VIEW_TILE),
                               tx * VIEW_TILE, min(w, (tx + 1) * VIEW_TILE), pool=False).nbytes
        return sum(self.pool.map(one, tiles))

    def reset(self):
        if self.cache is not None:
            self.cache.clear()


class TifProduct(_Reader):
    """The product's readers, unchanged: RawTileProvider for region / tile
    reads (512 pieces on the pool, as A0), TiffTileReader for the scan."""

    def __init__(self, tif):
        from block01.core.quant_sources import TiffTileReader
        self.inner = a0.TifReader(tif)
        self.pool = self.inner.pool
        self.scanner = TiffTileReader(tif, threads=THREADS)
        self.path = tif

    def level_shape(self, k):
        return self.inner.level_shape(k)

    def region(self, c, k, y0, y1, x0, x1, pool=True):
        return self.inner.region(c, k, y0, y1, x0, x1, pool=pool)

    def regions(self, chans, k, y0, y1, x0, x1):
        if k != 0:
            raise ValueError("scan is level 0")
        return self.scanner.read(chans, y0, y1, x0, x1)

    def files(self):
        return [self.path]


class TifAdapted(_Reader):
    """Tile-aligned direct TIFF decode: each covered TIFF tile's bytes are read
    (pread, one fd per thread) and decoded by tifffile on the pool; a 512 MiB
    decoded-tile cache; the same IO classes as NgffAdapted."""

    def __init__(self, tif):
        import tifffile
        self.path = tif
        self.tf = tifffile.TiffFile(tif)
        self.levels = []
        for lv in self.tf.series[0].levels:
            pages = [pg.aspage() if hasattr(pg, "aspage") else pg for pg in lv.pages]
            p = pages[0]
            if not getattr(p, "is_tiled", False):
                raise SystemExit("TifAdapted needs tiled levels")
            self.levels.append({"pages": pages, "shape": tuple(int(v) for v in lv.shape[-2:]),
                                "th": int(p.tilelength), "tw": int(p.tilewidth),
                                "dtype": np.dtype(p.dtype)})
        self.pool = ThreadPoolExecutor(THREADS, thread_name_prefix="tif-adapted")
        self.cache = BlockCache()
        self._local = threading.local()
        self._fds, self._lock = [], threading.Lock()

    def level_shape(self, k):
        return self.levels[k]["shape"]

    def _fd(self):
        fd = getattr(self._local, "fd", None)
        if fd is None:
            fd = os.open(self.path, os.O_RDONLY)
            self._local.fd = fd
            with self._lock:
                self._fds.append(fd)
        return fd

    def _tile(self, k, c, ty, tx):
        key = (k, c, ty, tx)
        blk = self.cache.get(key)
        if blk is not None:
            return blk
        L = self.levels[k]
        page = L["pages"][c]
        ntx = -(-L["shape"][1] // L["tw"])
        i = ty * ntx + tx
        cnt = int(page.databytecounts[i])
        if cnt == 0:
            blk = np.zeros((L["th"], L["tw"]), L["dtype"])
        else:
            data = os.pread(self._fd(), cnt, int(page.dataoffsets[i]))
            arr = page.decode(data, i, jpegtables=getattr(page, "jpegtables", None))[0]
            blk = np.asarray(arr).reshape(arr.shape[-3], arr.shape[-2]) if arr.ndim >= 3 else np.asarray(arr)
        self.cache.put(key, blk)
        return blk

    def _fill(self, out, c, k, y0, x0, box):
        ty, tx, by0, by1, bx0, bx1 = box
        L = self.levels[k]
        blk = self._tile(k, c, ty, tx)
        oy, ox = ty * L["th"], tx * L["tw"]
        out[by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] = blk[by0 - oy:by1 - oy, bx0 - ox:bx1 - ox]

    def region(self, c, k, y0, y1, x0, x1, pool=True):
        L = self.levels[k]
        out = np.empty((y1 - y0, x1 - x0), L["dtype"])
        bs = list(boxes(y0, y1, x0, x1, L["th"], L["tw"]))
        if pool:
            list(self.pool.map(lambda b: self._fill(out, c, k, y0, x0, b), bs))
        else:
            for b in bs:
                self._fill(out, c, k, y0, x0, b)
        return out

    def regions(self, chans, k, y0, y1, x0, x1):
        L = self.levels[k]
        out = np.empty((len(chans), y1 - y0, x1 - x0), L["dtype"])
        jobs = [(i, c, b) for i, c in enumerate(chans) for b in boxes(y0, y1, x0, x1, L["th"], L["tw"])]
        list(self.pool.map(lambda j: self._fill(out[j[0]], j[1], k, y0, x0, j[2]), jobs))
        return out

    def files(self):
        return [self.path]


class NgffGeneric(_Reader):
    """A0's reader: zarr slicing, chunk-split regions on the 8-thread pool."""

    def __init__(self, ngff):
        import zarr
        from numcodecs import blosc
        blosc.use_threads = False
        self.root = zarr.open_group(ngff, mode="r")
        self.base = ngff
        n = len(self.root.attrs["multiscales"][0]["datasets"])
        self.arrs = [self.root[str(k)] for k in range(n)]
        self.pool = ThreadPoolExecutor(THREADS, thread_name_prefix="ngff-generic")

    def level_shape(self, k):
        return tuple(self.arrs[k].shape[1:])

    def region(self, c, k, y0, y1, x0, x1, pool=True):
        arr = self.arrs[k]
        ch, cw = arr.chunks[1:]
        out = np.empty((y1 - y0, x1 - x0), arr.dtype)

        def one(b):
            _ty, _tx, by0, by1, bx0, bx1 = b
            out[by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] = arr[c, by0:by1, bx0:bx1]
        bs = list(boxes(y0, y1, x0, x1, ch, cw))
        list(self.pool.map(one, bs)) if pool else [one(b) for b in bs]
        return out

    def regions(self, chans, k, y0, y1, x0, x1):
        arr = self.arrs[k]
        ch, cw = arr.chunks[1:]
        out = np.empty((len(chans), y1 - y0, x1 - x0), arr.dtype)

        def one(j):
            i, c, (_ty, _tx, by0, by1, bx0, bx1) = j
            out[i, by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] = arr[c, by0:by1, bx0:bx1]
        list(self.pool.map(one, [(i, c, b) for i, c in enumerate(chans)
                                 for b in boxes(y0, y1, x0, x1, ch, cw)]))
        return out

    def files(self):
        return [os.path.join(d, f) for d, _, fs in os.walk(self.base) for f in fs]


class NgffAdapted(NgffGeneric):
    """Chunk-aligned direct decode: each covered chunk file's bytes are read
    and Blosc-decoded on the pool (the decode releases the GIL); a 512 MiB
    decoded-chunk cache; the same IO classes as TifAdapted."""

    def __init__(self, ngff):
        super().__init__(ngff)
        self.cache = BlockCache()
        self.codecs = [a.compressor for a in self.arrs]

    def _chunk(self, k, c, ty, tx):
        key = (k, c, ty, tx)
        blk = self.cache.get(key)
        if blk is not None:
            return blk
        arr = self.arrs[k]
        ch, cw = arr.chunks[1:]
        p = os.path.join(self.base, str(k), str(c), str(ty), str(tx))
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
            blk = np.frombuffer(self.codecs[k].decode(raw), arr.dtype).reshape(ch, cw)
        except FileNotFoundError:
            blk = np.zeros((ch, cw), arr.dtype)
        self.cache.put(key, blk)
        return blk

    def _fill(self, out, c, k, y0, x0, b):
        ty, tx, by0, by1, bx0, bx1 = b
        ch, cw = self.arrs[k].chunks[1:]
        blk = self._chunk(k, c, ty, tx)
        oy, ox = ty * ch, tx * cw
        out[by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] = blk[by0 - oy:by1 - oy, bx0 - ox:bx1 - ox]

    def region(self, c, k, y0, y1, x0, x1, pool=True):
        arr = self.arrs[k]
        out = np.empty((y1 - y0, x1 - x0), arr.dtype)
        bs = list(boxes(y0, y1, x0, x1, *arr.chunks[1:]))
        if pool:
            list(self.pool.map(lambda b: self._fill(out, c, k, y0, x0, b), bs))
        else:
            for b in bs:
                self._fill(out, c, k, y0, x0, b)
        return out

    def regions(self, chans, k, y0, y1, x0, x1):
        arr = self.arrs[k]
        out = np.empty((len(chans), y1 - y0, x1 - x0), arr.dtype)
        jobs = [(i, c, b) for i, c in enumerate(chans) for b in boxes(y0, y1, x0, x1, *arr.chunks[1:])]
        list(self.pool.map(lambda j: self._fill(out[j[0]], j[1], k, y0, x0, j[2]), jobs))
        return out


def make_reader(side, tif, ngff):
    if side == "tif_product":
        return TifProduct(tif)
    if side == "tif_adapted":
        return TifAdapted(tif)
    if side == "ngff_generic":
        return NgffGeneric(ngff)
    if side == "ngff_adapted":
        return NgffAdapted(ngff)
    raise SystemExit(side)


# ── oracle ───────────────────────────────────────────────────────────────


def cmd_oracle(args):
    """Every reader, every level, random windows: bitwise == the product's
    RawTileProvider (and the scan == TiffTileReader)."""
    meta = a0.tif_meta(args.tif)
    ref = TifProduct(args.tif)
    readers = {s: make_reader(s, args.tif, args.ngff) for s in
               (["tif_adapted"] + (["ngff_generic", "ngff_adapted"] if args.ngff else []))}
    rng = np.random.default_rng(a0.SEED + 1)
    bad = 0
    n = 0
    for k, (c_, h, w) in enumerate(meta["levels"]):
        for _ in range(args.windows):
            c = int(rng.integers(0, c_))
            y0, x0 = int(rng.integers(0, h - 1)), int(rng.integers(0, w - 1))
            y1, x1 = min(h, y0 + int(rng.integers(1, 3000))), min(w, x0 + int(rng.integers(1, 3000)))
            want = ref.region(c, k, y0, y1, x0, x1)
            for s, r in readers.items():
                n += 1
                if not np.array_equal(r.region(c, k, y0, y1, x0, x1), want):
                    bad += 1
                    print(f"[oracle] MISMATCH {s} level {k} c{c} {[y0, y1, x0, x1]}")
    h, w = meta["levels"][0][1:]
    for _ in range(4):
        y0, x0 = int(rng.integers(0, h - 4096)), int(rng.integers(0, w - 4096))
        chans = [int(v) for v in rng.choice(meta["levels"][0][0], 9, replace=False)]
        want = ref.regions(chans, 0, y0, y0 + 4096, x0, x0 + 4096)
        for s, r in readers.items():
            n += 1
            if not np.array_equal(r.regions(chans, 0, y0, y0 + 4096, x0, x0 + 4096), want):
                bad += 1
                print(f"[oracle] SCAN MISMATCH {s}")
    print(f"[oracle] {n} comparisons, {bad} mismatches -> {'PASS' if bad == 0 else 'FAIL'}")
    sys.stdout.flush()
    os._exit(0 if bad == 0 else 1)


# ── timed runs ───────────────────────────────────────────────────────────


def _runs(fn, files, reset):
    """A0's protocol: 1 untimed warm-up, 3 cold (evicted + fincore), 3 warm."""
    out = []

    def one(mode):
        res = None
        if mode == "cold":
            a0.evict(files)
            res = a0.resident_bytes(files, sample=None if len(files) < 20000 else 5000)
        reset()
        t0 = time.perf_counter()
        c0 = os.times()
        extra = fn()
        wall = time.perf_counter() - t0
        c1 = os.times()
        r = {"mode": mode, "resident_bytes_before": res, "total_s": round(wall, 4),
             "cpu_utilisation": round((c1.user + c1.system - c0.user - c0.system) / wall, 2)}
        r.update(extra or {})
        return r
    one("warm")
    out += [one("cold") for _ in range(a0.REPS)]
    one("warm")
    out += [one("warm") for _ in range(a0.REPS)]
    summary = {}
    for mode in ("cold", "warm"):
        rs = [r for r in out if r["mode"] == mode]
        summary[mode] = {"total_s_median": float(np.median([r["total_s"] for r in rs])),
                         "max_resident_before": max((r["resident_bytes_before"] or 0) for r in rs)}
        if "op_median_ms" in rs[0]:
            summary[mode]["op_median_ms_median"] = float(np.median([r["op_median_ms"] for r in rs]))
    return out, summary


def cmd_bench(args):
    meta = a0.tif_meta(args.tif)
    shape0 = tuple(meta["levels"][0][1:])
    reader = make_reader(args.side, args.tif, args.ngff)
    level_shapes = [reader.level_shape(k) for k in range(len(meta["levels"]))]
    ops = a0.plan(args.workload, args.tif, shape0, level_shapes, meta["levels"][0][0])
    adapted = args.side.endswith("adapted")
    ahead = ThreadPoolExecutor(1, thread_name_prefix="read-ahead") if adapted else None

    def run_ops():
        times, nbytes = [], 0
        pending = None
        for i, op in enumerate(ops):
            ts = time.perf_counter()
            if op[0] == "step4":
                _, chans, y0, y1, x0, x1 = op
                if adapted:
                    arr = pending.result() if pending is not None else reader.regions(chans, 0, y0, y1, x0, x1)
                    nxt = ops[i + 1] if i + 1 < len(ops) else None
                    pending = ahead.submit(reader.regions, nxt[1], 0, *nxt[2:]) if nxt else None
                else:
                    arr = reader.regions(chans, 0, y0, y1, x0, x1)
                nbytes += arr.nbytes
            elif op[0] == "region":
                _, c, k, y0, y1, x0, x1 = op
                nbytes += reader.region(c, k, y0, y1, x0, x1).nbytes
            elif op[0] == "tiles":
                _, c, k, tiles = op
                nbytes += reader.tiles(c, k, tiles)
            else:
                raise ValueError(op[0])
            times.append((time.perf_counter() - ts) * 1000.0)
        t = np.asarray(times)
        return {"op_median_ms": round(float(np.median(t)), 3),
                "op_p95_ms": round(float(np.percentile(t, 95)), 3), "bytes_decoded": int(nbytes)}

    runs, summary = _runs(run_ops, reader.files(), reader.reset)
    result = {"what": "bench", "side": args.side, "workload": args.workload, "tif": args.tif,
              "ngff": args.ngff, "budget": BUDGET if args.side != "tif_product" else
              {"threads": THREADS, "note": "product readers as they are"},
              "n_ops": len(ops), "runs": runs, **summary, "peak_rss_mb": round(a0.peak_rss_mb())}
    json.dump(result, open(args.out, "w"), indent=1)
    print(f"[bench] {args.side} {args.workload}: cold {summary['cold']['total_s_median']:.3f}s "
          f"warm {summary['warm']['total_s_median']:.3f}s rss {result['peak_rss_mb']} MB", flush=True)
    sys.stdout.flush()
    os._exit(0)


# ── Step4 end to end ─────────────────────────────────────────────────────


class _ScanShim:
    """`quant_sources.TiffTileReader`'s surface over another reader, so the
    product's Step4 runs unchanged with only the raw reader swapped."""

    def __init__(self, reader, mode):
        self.r = reader
        self.dtype = np.dtype(np.uint8)
        self.shape = reader.level_shape(0)
        self.mode = mode
        self._pool = ThreadPoolExecutor(THREADS, thread_name_prefix="step4-corrected")

    def read(self, indices, y0, y1, x0, x1):
        return self.r.regions(list(indices), 0, y0, y1, x0, x1)

    def close(self):
        self._pool.shutdown(wait=True)


def cmd_step4(args):
    from block01.core import quant_sources as qs
    from block01.workers import feature_extract_worker as few
    prov = json.load(open(os.path.join(args.run, "..", "..", "..", "step4", "quantification_runs",
                                       os.path.basename(args.run.rstrip("/")), "Full_WSI",
                                       "cell_features_provenance.json")))
    stats = prov["statistics"]
    reader = None
    real = qs.TiffTileReader
    if args.side != "tif_product":
        reader = make_reader(args.side, args.tif, args.ngff)
        qs.TiffTileReader = lambda slide, threads=8: _ScanShim(reader, args.side)
    job = qs.resolve_quant_job(args.run, "Full WSI", None)
    store_files = []
    for p in [job.label_path, job.nucleus_path, job.table_path] + \
            [ch.path for ch in job.channels if ch.kind == "corrected"]:
        if p and os.path.isdir(p):
            store_files += [os.path.join(d, f) for d, _, fs in os.walk(p) for f in fs]
    files = (reader.files() if reader is not None else [args.tif]) + store_files
    work = args.keep or os.path.join(os.path.dirname(args.out), f"step4_{args.side}")
    _no_write(work)

    def run():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        few.run_extraction(args.run, work, roi_name="Full WSI", statistics=stats, write_csv=True)
        return {}
    runs, summary = _runs(run, files, reader.reset if reader is not None else (lambda: None))
    qs.TiffTileReader = real
    result = {"what": "step4_e2e", "side": args.side, "ngff": args.ngff, "statistics": stats,
              "budget": BUDGET if args.side != "tif_product" else {"note": "product Step4 as it is"},
              "outputs": work, "runs": runs, **summary, "peak_rss_mb": round(a0.peak_rss_mb())}
    json.dump(result, open(args.out, "w"), indent=1)
    print(f"[step4] {args.side}: cold {summary['cold']['total_s_median']:.2f}s "
          f"warm {summary['warm']['total_s_median']:.2f}s -> {work}", flush=True)
    sys.stdout.flush()
    os._exit(0)


# The only h5ad dataset that records the reader, backend, paths, timings or
# commit: the provenance JSON. Everything else -- X, layers, obs, obsm, var and
# the other uns fields -- is compared bitwise.
PROVENANCE_ONLY = ("uns/provenance_json",)


def cmd_same_step4(args):
    """The scientific payload bitwise: every h5ad dataset outside the
    provenance group and the CSV's numbers, in order."""
    import h5py
    import pandas as pd
    fails, excluded = [], set()
    ha = [f for f in os.listdir(args.a) if f.endswith(".h5ad")][0]
    with h5py.File(os.path.join(args.a, ha), "r") as fa, h5py.File(os.path.join(args.b, ha), "r") as fb:
        names = []
        fa.visititems(lambda n, o: names.append(n) if isinstance(o, h5py.Dataset) else None)
        nb = []
        fb.visititems(lambda n, o: nb.append(n) if isinstance(o, h5py.Dataset) else None)
        if sorted(names) != sorted(nb):
            fails.append(("dataset sets differ", sorted(set(names) ^ set(nb))))
        for n in names:
            if n.startswith(PROVENANCE_ONLY):
                excluded.add(n)
                continue
            if not np.array_equal(fa[n][()], fb[n][()]):
                fails.append(("h5ad", n))
    ca = [f for f in os.listdir(args.a) if f.endswith(".csv")][0]
    da, db = pd.read_csv(os.path.join(args.a, ca)), pd.read_csv(os.path.join(args.b, ca))
    if list(da.columns) != list(db.columns) or not da.equals(db):
        fails.append(("csv", "differs"))
    print(json.dumps({"same": not fails, "fails": fails[:20], "excluded": sorted(excluded)}, indent=1))
    sys.stdout.flush()
    os._exit(0 if not fails else 1)


# ── Step1 fusion, the product's FullFusionWorker ─────────────────────────


class _LoaderShim:
    """What FullFusionWorker needs of OMETIFFLoader, over another reader."""

    def __init__(self, reader, tif):
        from block01.core.io_loader import OMETIFFLoader
        real = OMETIFFLoader(tif)
        self.r = reader
        self.filepath = tif
        self.ch_map = dict(real.ch_map)
        self.shape = real.shape
        self.name_map = real.name_map

    def channel_names(self):
        return list(self.ch_map)

    def read_region(self, ch, y0, y1, x0, x1, downsample=1, correction_config=None, normalize=True):
        if normalize or downsample != 1:
            raise ValueError("the fusion path reads normalize=False, downsample=1")
        return self.r.region(self.ch_map[ch], 0, y0, y1, x0, x1).astype(np.float32)


def cmd_fusion(args):
    from PyQt5 import QtCore
    QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    from block01.core.io_loader import OMETIFFLoader
    from block01.ui.step0.overview_panel import FullFusionWorker
    from block01.utils.channel_remap_config import load_channel_remap_config
    ws = os.path.join(args.copy, "rois", json.load(open(os.path.join(args.copy, "A0_COPY_INFO.json")))["workspace"])
    settings = json.load(open(os.path.join(ws, "step1", "step1_fusion_settings.json")))
    handoff = json.load(open(os.path.join(ws, "step0", "step0_roi_result.json")))
    remap = load_channel_remap_config(handoff["channel_remap_config_path"])
    params = {str(n): dict(p) for n, p in (remap.get("channels") or {}).items()}
    rois = json.load(open(os.path.join(ws, "step1", "roi_config.json")))     # a list of ROI dicts
    reader = None
    if args.side == "tif_product":
        loader = OMETIFFLoader(args.tif)
    else:
        reader = make_reader(args.side, args.tif, args.ngff)
        loader = _LoaderShim(reader, args.tif)
    work = args.keep or os.path.join(os.path.dirname(args.out), f"fusion_{args.side}")
    _no_write(work)
    files = reader.files() if reader is not None else [args.tif]

    def run():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        cfg = dict(settings["fusion_config"])
        cfg.update({"ome_tiff": args.tif, "output_dir": work, "channel_remap_params": params,
                    "artifact_kind": "a2b_probe", "config_hash": "a2b_probe"})
        worker = FullFusionWorker(loader=loader, fusion_cfg=cfg, n_rows=2, n_cols=2,
                                  rois=rois or None)
        errs = []
        worker.error.connect(errs.append)
        worker.run()
        if errs:
            raise RuntimeError(errs[0])
        return {}
    runs, summary = _runs(run, files, reader.reset if reader is not None else (lambda: None))
    result = {"what": "fusion_full", "side": args.side, "ngff": args.ngff, "outputs": work,
              "remap_channels": sorted(params), "runs": runs, **summary,
              "peak_rss_mb": round(a0.peak_rss_mb())}
    json.dump(result, open(args.out, "w"), indent=1)
    print(f"[fusion] {args.side}: cold {summary['cold']['total_s_median']:.2f}s "
          f"warm {summary['warm']['total_s_median']:.2f}s -> {work}", flush=True)
    sys.stdout.flush()
    os._exit(0)


def cmd_same_fused(args):
    import zarr

    def arrays(p):
        out = {}
        for d, _, fs in os.walk(p):
            if ".zarray" in fs:
                rel = os.path.relpath(d, p)
                out[rel] = zarr.open(d, mode="r")
        return out
    a, b = arrays(args.a), arrays(args.b)
    fails = [] if sorted(a) == sorted(b) else [("array sets differ", sorted(set(a) ^ set(b)))]
    if not a:
        fails.append("no fused array found -- nothing was compared")
    for k in sorted(set(a) & set(b)):
        if a[k].shape != b[k].shape or not np.array_equal(a[k][...], b[k][...]):
            fails.append(k)
    nonzero = {k: bool(np.any(a[k][...])) for k in a}
    fails += [f"{k} is all zero -- a degenerate fusion proves nothing" for k, v in nonzero.items() if not v]
    print(json.dumps({"same": not fails, "fails": fails, "arrays": sorted(a), "nonzero": nonzero}, indent=1))
    sys.stdout.flush()
    os._exit(0 if not fails else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("--src", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--chunk", type=int, choices=[512, 1024, 2048], required=True)
    p.add_argument("--codec", choices=["lz4", "zstd"], required=True)
    p.add_argument("--shuffle", choices=["byte", "bit"], default="byte")
    p = sub.add_parser("verify")
    p.add_argument("ngff")
    p.add_argument("--src", required=True)
    p = sub.add_parser("oracle")
    p.add_argument("--tif", required=True)
    p.add_argument("--ngff")
    p.add_argument("--windows", type=int, default=12)
    p = sub.add_parser("bench")
    p.add_argument("--tif", required=True)
    p.add_argument("--ngff")
    p.add_argument("--side", choices=SIDES, required=True)
    p.add_argument("--workload", choices=BENCH_WORKLOADS, required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("step4")
    p.add_argument("--tif", required=True)
    p.add_argument("--ngff")
    p.add_argument("--side", choices=SIDES, required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--keep")
    p = sub.add_parser("fusion")
    p.add_argument("--tif", required=True)
    p.add_argument("--ngff")
    p.add_argument("--side", choices=SIDES, required=True)
    p.add_argument("--copy", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--keep")
    p = sub.add_parser("same-step4")
    p.add_argument("a")
    p.add_argument("b")
    p = sub.add_parser("same-fused")
    p.add_argument("a")
    p.add_argument("b")
    args = ap.parse_args()
    return {"ingest": cmd_ingest, "verify": cmd_verify, "oracle": cmd_oracle, "bench": cmd_bench,
            "step4": cmd_step4, "same-step4": cmd_same_step4, "fusion": cmd_fusion,
            "same-fused": cmd_same_fused}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
