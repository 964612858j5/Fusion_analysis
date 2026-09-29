"""
Block A0-2 of v16 (docs/v16_A0_application.md): OME-TIFF vs an OME-NGFF 0.4
copy written with the installed zarr 2.18. A benchmark, not imported by the
product; the NGFF writer and reader here are throwaway candidates, NOT the
product's NgffSource / ingest (block A2b).

    python scripts/bench_v16_a0_storage.py ingest  --src TIF --out DIR.ome.zarr [--codec lz4|zstd]
    python scripts/bench_v16_a0_storage.py check   DIR.ome.zarr --src TIF
    python scripts/bench_v16_a0_storage.py run     --tif TIF --ngff DIR --fmt tif|ngff --workload W --out JSON
    python scripts/bench_v16_a0_storage.py report  JSON...

PRE-REGISTERED (application v2, §3 A0-2 step 2; do not change after the
first run without telling the user):
  seed 16; one untimed warm-up, then 3 cold + 3 warm runs per workload;
  cold = posix_fadvise(DONTNEED) on every file of the format, then fincore
  must report 0 resident bytes; warm = right after a full run;
  per-operation median and p95; 8 threads for both formats; Blosc's own
  threads off for the NGFF reader (the pool is the concurrency).
Workloads:
  roi512 / roi2048 / roi4096  level 0, 30 ROIs each, centres uniform in the
                              level-2 DAPI>0 tissue mask, one channel each
                              from a fixed seeded sequence
  viewport_l0 / _l1 / _l2     1600x1000 logical px at scale 1 / 0.25 /
                              0.0625 (level by pick_display_level), start at
                              the tissue centre, 10 steps right + 10 down of
                              25 % of the viewport; only newly visible 512
                              tiles are read (earlier ones count as cached)
  channel_switch              level 1, fixed viewport at the tissue centre,
                              channels [0,5,12,17,3,22,8,26,14,1]
  seq29                       4096^2 window at the tissue centre, level 0,
                              channels 0..28 in order
  fusion                      8192^2 region at the tissue centre, 2x2
                              blocks, 6 channels (DAPI + 5 fixed markers)
                              per block, 8 threads (the FullFusionWorker
                              pattern; TIFF via OMETIFFLoader.read_region,
                              which reopens the TiffFile per call)
  step4                       whole image, level 0, 4096^2 tiles row-major,
                              29 channels in batches of 16 (256 MiB of
                              uint8 per batch, QuantSettings.batch_bytes),
                              TiffTileReader(threads=8) for TIFF
  derived_l1                  the alternative "raw stays OME-TIFF, only
                              derived products use NGFF": one Step0-corrected
                              channel (float32) shown at level 1 along the
                              viewport_l1 path. fmt tif = TODAY (the
                              product's reduce_corrected from level 0, stride
                              round(level_downsample) = 4, per 512 level-1
                              tile); fmt ngff = a stored level-1 array built
                              once with the same reduce_corrected (derived-
                              ingest), read per tile. 8 threads both.
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import numpy as np

OME_NS = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
THREADS = 8
SEED = 16
REPS = 3
CHUNK = 512
SWITCH_SEQ = [0, 5, 12, 17, 3, 22, 8, 26, 14, 1]
FUSION_CHANNELS = [0, 5, 12, 15, 17, 19]           # DAPI, CD68, CD4, CD3D, CD8, HLA-DR
WORKLOADS = ["derived_l1", "roi512", "roi2048", "roi4096", "viewport_l0", "viewport_l1", "viewport_l2",
             "channel_switch", "seq29", "fusion", "step4"]


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# ── NGFF 0.4 candidate: writer + self-check ──────────────────────────────


def tif_meta(path):
    import tifffile
    with tifffile.TiffFile(path) as tif:
        s = tif.series[0]
        root = ET.fromstring(tif.ome_metadata)
        pix = root.find(".//ome:Pixels", OME_NS)
        chans = root.findall(".//ome:Channel", OME_NS)
        return {"levels": [[int(v) for v in lv.shape] for lv in s.levels],
                "dtype": str(s.dtype), "names": [c.get("Name") for c in chans],
                "colors": [c.get("Color") for c in chans],
                "psx": float(pix.get("PhysicalSizeX")) if pix.get("PhysicalSizeX") else None,
                "psy": float(pix.get("PhysicalSizeY")) if pix.get("PhysicalSizeY") else None,
                "unit": pix.get("PhysicalSizeXUnit")}


def ngff_attrs(meta):
    """multiscales + omero for NGFF 0.4. scale = physical size x the level's
    REAL shape ratio when the source has a physical size (unit micrometer);
    otherwise the dimensionless ratio and no unit is declared."""
    c, h0, w0 = meta["levels"][0]
    physical = meta["psx"] is not None and meta["psy"] is not None and meta["unit"] in ("µm", "um", "micrometer")
    axes = [{"name": "c", "type": "channel"},
            {"name": "y", "type": "space"}, {"name": "x", "type": "space"}]
    if physical:
        axes[1]["unit"] = axes[2]["unit"] = "micrometer"
    datasets = []
    for k, (_c, h, w) in enumerate(meta["levels"]):
        ry, rx = h0 / h, w0 / w
        sy, sx = (meta["psy"] * ry, meta["psx"] * rx) if physical else (ry, rx)
        datasets.append({"path": str(k), "coordinateTransformations": [
            {"type": "scale", "scale": [1.0, sy, sx]}]})
    omero = {"version": "0.4", "channels": [
        {"label": n, "color": "%06X" % (int(col) >> 8 & 0xFFFFFF) if col is not None else "FFFFFF",
         "active": True, "window": {"min": 0, "max": 255, "start": 0, "end": 255}}
        for n, col in zip(meta["names"], meta["colors"])]}
    return {"multiscales": [{"version": "0.4", "name": "image", "axes": axes, "datasets": datasets,
                             "type": "mean (pyramid levels copied from the OME-TIFF)"}],
            "omero": omero}, physical


def cmd_ingest(args):
    import tifffile
    import zarr
    from numcodecs import Blosc
    if os.path.realpath(args.out).startswith(os.path.realpath(os.path.expanduser("~/fusion_data"))):
        raise SystemExit("never write under ~/fusion_data")
    if os.path.exists(args.out):
        raise SystemExit(f"{args.out} exists")
    meta = tif_meta(args.src)
    attrs, physical = ngff_attrs(meta)
    comp = Blosc(cname=args.codec, clevel=5, shuffle=Blosc.SHUFFLE)
    t0 = time.perf_counter()
    cpu0 = os.times()
    store = zarr.DirectoryStore(args.out, dimension_separator="/")
    root = zarr.group(store=store)
    root.attrs.update(attrs)
    tif = tifffile.TiffFile(args.src)
    for k, lv in enumerate(tif.series[0].levels):
        arr = root.create_dataset(str(k), shape=tuple(lv.shape), chunks=(1, CHUNK, CHUNK),
                                  dtype=lv.dtype, compressor=comp, fill_value=0,
                                  dimension_separator="/")
        for c in range(lv.shape[0]):
            plane = lv.pages[c].asarray(maxworkers=THREADS)
            arr[c] = plane
            del plane
        print(f"[ingest] level {k} {lv.shape} done {time.perf_counter() - t0:.1f}s, "
              f"peak RSS {peak_rss_mb():.0f} MB", flush=True)
    tif.close()
    cpu1 = os.times()
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(args.out) for f in fs)
    nfiles = sum(len(fs) for _, _, fs in os.walk(args.out))
    info = {"src": args.src, "out": args.out, "codec": args.codec, "physical_units": physical,
            "seconds": round(time.perf_counter() - t0, 1),
            "cpu_utilisation": round((cpu1.user + cpu1.system - cpu0.user - cpu0.system)
                                     / (time.perf_counter() - t0), 2),
            "bytes": size, "files": nfiles, "source_bytes": os.path.getsize(args.src),
            "ratio_to_source": round(size / os.path.getsize(args.src), 3),
            "peak_rss_mb": round(peak_rss_mb())}
    with open(args.out.rstrip("/") + ".ingest.json", "w") as fh:
        json.dump(info, fh, indent=1)
    print(json.dumps(info, indent=1))


def cmd_check(args):
    """Minimal NGFF 0.4 self-check of what we wrote (no new dependency)."""
    import zarr
    meta = tif_meta(args.src)
    root = zarr.open_group(args.ngff, mode="r")
    fails = []

    def ok(cond, what):
        print(f"[check] {'OK  ' if cond else 'FAIL'} {what}")
        if not cond:
            fails.append(what)

    ms = root.attrs.get("multiscales")
    ok(isinstance(ms, list) and len(ms) == 1, "one multiscales entry")
    m = ms[0]
    ok(m.get("version") == "0.4", "multiscales.version == 0.4")
    axes = m.get("axes", [])
    ok([a.get("name") for a in axes] == ["c", "y", "x"], "axes names c, y, x (in order)")
    ok([a.get("type") for a in axes] == ["channel", "space", "space"], "axes types channel/space/space")
    physical = meta["psx"] is not None
    if physical:
        ok(axes[1].get("unit") == "micrometer" and axes[2].get("unit") == "micrometer"
           and "unit" not in axes[0], "space axes unit micrometer, channel axis no unit")
    else:
        ok(all("unit" not in a for a in axes), "no unit declared without a physical size")
    ds = m.get("datasets", [])
    ok([d.get("path") for d in ds] == [str(k) for k in range(len(meta["levels"]))], "datasets paths 0..n-1")
    h0, w0 = meta["levels"][0][1:]
    for k, d in enumerate(ds):
        arr = root[d["path"]]
        ok(list(arr.shape) == meta["levels"][k], f"level {k} shape {list(arr.shape)} == TIFF level")
        ok(arr.chunks == (1, CHUNK, CHUNK), f"level {k} chunks (1, {CHUNK}, {CHUNK})")
        ok(getattr(arr._store, "_dimension_separator", "/") == "/" or arr._dimension_separator == "/",
           f"level {k} dimension_separator '/'")
        ct = d.get("coordinateTransformations", [])
        ok(len(ct) == 1 and ct[0].get("type") == "scale" and len(ct[0].get("scale", [])) == 3,
           f"level {k}: exactly one scale transform of length 3")
        sc = ct[0]["scale"]
        h, w = meta["levels"][k][1:]
        base_y, base_x = (meta["psy"], meta["psx"]) if physical else (1.0, 1.0)
        ok(sc[0] == 1.0, f"level {k}: channel scale 1")
        ok(abs(sc[1] / base_y - h0 / h) < 1e-12 and abs(sc[2] / base_x - w0 / w) < 1e-12,
           f"level {k}: scale / level-0 scale = real shape ratio ({h0 / h:.6f}, {w0 / w:.6f})")
    if physical:
        s0 = ds[0]["coordinateTransformations"][0]["scale"]
        ok(s0[1] == meta["psy"] and s0[2] == meta["psx"], "level 0 scale = PhysicalSizeY/X")
    om = root.attrs.get("omero", {})
    ok([c.get("label") for c in om.get("channels", [])] == meta["names"], "omero channel labels = OME names")
    # pixels: a few windows per level bitwise equal to the TIFF
    import tifffile
    rng = np.random.default_rng(SEED)
    with tifffile.TiffFile(args.src) as tif:
        for k, lv in enumerate(tif.series[0].levels):
            zt = zarr.open(tif.series[0].aszarr(level=k), mode="r")
            arr = root[str(k)]
            _c, h, w = meta["levels"][k]
            bad = 0
            for _ in range(8):
                c = int(rng.integers(0, _c))
                y, x = int(rng.integers(0, max(1, h - 300))), int(rng.integers(0, max(1, w - 300)))
                if not np.array_equal(zt[c, y:y + 300, x:x + 300], arr[c, y:y + 300, x:x + 300]):
                    bad += 1
            ok(bad == 0, f"level {k}: 8 sampled windows bitwise equal to the TIFF")
    print(f"[check] {'PASS' if not fails else 'FAIL'} ({len(fails)} failures)")
    return 0 if not fails else 1


# ── derived products (the alternative) ───────────────────────────────────


def corrected_region(corrected_zarr, roi, channel):
    from block01.utils.calibration_source import open_corrected_channel_array
    from block01.viewer.step1_source import CorrectedRegion
    arr = open_corrected_channel_array(corrected_zarr, channel, roi)
    if arr is None:
        raise SystemExit(f"no corrected {channel} for {roi} in {corrected_zarr}")
    bbox = arr.attrs.get("roi_bbox_fullres") or arr.attrs.get("bbox_fullres")
    return CorrectedRegion(arr, bbox)


def cmd_derived_ingest(args):
    import zarr
    from numcodecs import Blosc
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from block01.viewer.step1_source import reduce_corrected
    if os.path.realpath(args.corrected).startswith(os.path.realpath(os.path.expanduser("~/fusion_data"))):
        raise SystemExit("read the COPY's corrected zarr, not the project's")
    region = corrected_region(args.corrected, args.roi, args.channel)
    y0, y1, x0, x1 = region.bbox
    t0 = time.perf_counter()
    root = zarr.group(store=zarr.DirectoryStore(args.out, dimension_separator="/"))
    for k, stride in ((1, 4), (2, 16)):
        mean, valid = reduce_corrected(region, (y0, y1, x0, x1), stride)
        arr = root.create_dataset(str(k), data=np.asarray(mean, np.float32), chunks=(CHUNK, CHUNK),
                                  compressor=Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE),
                                  dimension_separator="/", overwrite=True)
        arr.attrs.update({"stride": stride, "valid_fraction": float(np.mean(valid))})
    root.attrs.update({"channel": args.channel, "roi": args.roi, "source": args.corrected,
                       "bbox": [y0, y1, x0, x1]})
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(args.out) for f in fs)
    info = {"seconds": round(time.perf_counter() - t0, 2), "bytes": size, "peak_rss_mb": round(peak_rss_mb())}
    json.dump(info, open(args.out.rstrip("/") + ".ingest.json", "w"), indent=1)
    print(json.dumps(info))


# ── readers (equal concurrency) ──────────────────────────────────────────


def chunk_boxes(y0, y1, x0, x1, size=CHUNK):
    for ty in range(y0 // size, (y1 - 1) // size + 1):
        for tx in range(x0 // size, (x1 - 1) // size + 1):
            yield (max(y0, ty * size), min(y1, (ty + 1) * size),
                   max(x0, tx * size), min(x1, (tx + 1) * size))


class NgffReader:
    """Candidate: arrays opened once; a region is split on chunk boundaries
    and the pieces are read on an 8-thread pool; Blosc's threads off."""

    def __init__(self, path):
        import zarr
        from numcodecs import blosc
        blosc.use_threads = False
        self.root = zarr.open_group(path, mode="r")
        self.levels = [self.root[str(k)] for k in range(len(self.root.attrs["multiscales"][0]["datasets"]))]
        self.pool = ThreadPoolExecutor(THREADS)

    def level_shape(self, k):
        return tuple(self.levels[k].shape[1:])

    def region(self, c, k, y0, y1, x0, x1, pool=True):
        arr = self.levels[k]
        out = np.empty((y1 - y0, x1 - x0), arr.dtype)

        def one(b):
            by0, by1, bx0, bx1 = b
            out[by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] = arr[c, by0:by1, bx0:bx1]
        boxes = list(chunk_boxes(y0, y1, x0, x1))
        if pool:
            list(self.pool.map(one, boxes))
        else:
            for b in boxes:
                one(b)
        return out

    def regions(self, channels, k, y0, y1, x0, x1):
        """Several channels of one region, all chunks on the one pool."""
        arr = self.levels[k]
        out = np.empty((len(channels), y1 - y0, x1 - x0), arr.dtype)
        jobs = [(i, c, b) for i, c in enumerate(channels) for b in chunk_boxes(y0, y1, x0, x1)]

        def one(j):
            i, c, (by0, by1, bx0, bx1) = j
            out[i, by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] = arr[c, by0:by1, bx0:bx1]
        list(self.pool.map(one, jobs))
        return out

    def files(self):
        return [os.path.join(d, f) for d, _, fs in os.walk(self.root.store.path) for f in fs]


class NgffDirectReader(NgffReader):
    """SUPPLEMENTARY (not pre-registered; never used for the adoption rule):
    the NGFF analogue of TiffTileReader -- read each chunk's bytes from its
    file and Blosc-decode them on the pool (the decode releases the GIL), so
    zarr-python's per-chunk Python work is out of the timed path. Answers
    whether a gap is the FORMAT or the READER."""

    def __init__(self, path):
        super().__init__(path)
        from numcodecs import Blosc
        self.codec = Blosc()
        self.base = path

    def _chunk(self, k, c, ty, tx, shape):
        p = os.path.join(self.base, str(k), str(c), str(ty), str(tx))
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return np.zeros(shape, np.uint8)
        return np.frombuffer(self.codec.decode(raw), np.uint8).reshape(CHUNK, CHUNK)

    def regions(self, channels, k, y0, y1, x0, x1):
        out = np.empty((len(channels), y1 - y0, x1 - x0), np.uint8)
        jobs = [(i, c, b) for i, c in enumerate(channels) for b in chunk_boxes(y0, y1, x0, x1)]

        def one(j):
            i, c, (by0, by1, bx0, bx1) = j
            ty, tx = by0 // CHUNK, bx0 // CHUNK
            blk = self._chunk(k, c, ty, tx, (CHUNK, CHUNK))
            oy, ox = ty * CHUNK, tx * CHUNK
            out[i, by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] = blk[by0 - oy:by1 - oy, bx0 - ox:bx1 - ox]
        list(self.pool.map(one, jobs))
        return out

    def region(self, c, k, y0, y1, x0, x1, pool=True):
        return self.regions([c], k, y0, y1, x0, x1)[0]


class TifReader:
    """The product's readers: RawTileProvider (viewer / ROI), OMETIFFLoader
    (fusion), TiffTileReader (Step4); 512 tiles on an 8-thread pool."""

    def __init__(self, path):
        from block01.viewer.raw_tile_provider import RawTileProvider
        self.path = path
        self.provider = RawTileProvider(path)
        self.pool = ThreadPoolExecutor(THREADS)

    def level_shape(self, k):
        return tuple(self.provider.level_shape(k))

    def region(self, c, k, y0, y1, x0, x1, pool=True):
        out = None

        def one(b):
            by0, by1, bx0, bx1 = b
            arr, _off = self.provider.read_region(c, k, by0, by1, bx0, bx1)
            return b, arr
        boxes = list(chunk_boxes(y0, y1, x0, x1))
        results = self.pool.map(one, boxes) if pool else map(one, boxes)
        for (by0, by1, bx0, bx1), arr in results:
            if out is None:
                out = np.empty((y1 - y0, x1 - x0), arr.dtype)
            out[by0 - y0:by1 - y0, bx0 - x0:bx1 - x0] = arr
        return out

    def files(self):
        return [self.path]


# ── cache control ────────────────────────────────────────────────────────


def evict(files):
    os.sync()          # dirty pages cannot be dropped: flush them first
    for p in files:
        try:
            fd = os.open(p, os.O_RDONLY)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fd)
        except OSError:
            pass


def resident_bytes(files, sample=None):
    """fincore over the files (all, or an evenly spaced sample)."""
    fl = files if sample is None or len(files) <= sample else files[::max(1, len(files) // sample)]
    total = 0
    for i in range(0, len(fl), 2000):
        out = subprocess.run(["fincore", "--bytes", "--noheadings", "--output", "RES"] + fl[i:i + 2000],
                             capture_output=True, text=True).stdout.split()
        total += sum(int(v) for v in out if v.isdigit())
    return total


# ── workloads ────────────────────────────────────────────────────────────


def tissue_centres(tif_path, n, size, rng, shape0):
    """Centres uniform over the level-2 DAPI>0 mask, ROI kept inside."""
    import tifffile
    with tifffile.TiffFile(tif_path) as tif:
        lv = tif.series[0].levels[-1]
        m = lv.pages[0].asarray() > 0
    h0, w0 = shape0
    ry, rx = h0 / m.shape[0], w0 / m.shape[1]
    ys, xs = np.nonzero(m)
    out = []
    while len(out) < n:
        i = int(rng.integers(0, len(ys)))
        cy = int((ys[i] + rng.random()) * ry)
        cx = int((xs[i] + rng.random()) * rx)
        y0, x0 = cy - size // 2, cx - size // 2
        if 0 <= y0 and y0 + size <= h0 and 0 <= x0 and x0 + size <= w0:
            out.append((y0, x0))
    return out


def tissue_centre(tif_path, shape0):
    import tifffile
    with tifffile.TiffFile(tif_path) as tif:
        m = tif.series[0].levels[-1].pages[0].asarray() > 0
    ys, xs = np.nonzero(m)
    return int(np.median(ys) * shape0[0] / m.shape[0]), int(np.median(xs) * shape0[1] / m.shape[1])


def plan(workload, tif_path, shape0, level_shapes, n_channels):
    """The pre-registered list of operations: each is (label, callable-args)."""
    rng = np.random.default_rng(SEED)
    cy, cx = tissue_centre(tif_path, shape0)
    ops = []
    if workload == "derived_l1":
        vp = plan("viewport_l1", tif_path, shape0, level_shapes, n_channels)
        return [("derived", 1, op[3]) for op in vp]
    if workload.startswith("roi"):
        size = int(workload[3:])
        chans = rng.integers(0, n_channels, 30)
        for (y0, x0), c in zip(tissue_centres(tif_path, 30, size, rng, shape0), chans):
            ops.append(("region", int(c), 0, y0, y0 + size, x0, x0 + size))
    elif workload.startswith("viewport_l"):
        k = int(workload[-1])
        scale = {0: 1.0, 1: 0.25, 2: 0.0625}[k]
        h, w = level_shapes[k]
        dsy, dsx = shape0[0] / h, shape0[1] / w
        vw, vh = 1600 / scale / dsx, 1000 / scale / dsy      # viewport in level-k px
        y, x = cy / dsy - vh / 2, cx / dsx - vw / 2
        seen = set()
        path = [(0, 0)] + [(0, 1)] * 10 + [(1, 0)] * 10
        for dy, dx in path:
            y += dy * 0.25 * vh
            x += dx * 0.25 * vw
            y0, y1 = max(0, int(y)), min(h, int(y + vh) + 1)
            x0, x1 = max(0, int(x)), min(w, int(x + vw) + 1)
            tiles = [(ty, tx) for ty in range(y0 // CHUNK, (y1 - 1) // CHUNK + 1)
                     for tx in range(x0 // CHUNK, (x1 - 1) // CHUNK + 1) if (ty, tx) not in seen]
            seen.update(tiles)
            ops.append(("tiles", 0, k, tiles))
    elif workload == "channel_switch":
        k = 1
        h, w = level_shapes[k]
        dsy, dsx = shape0[0] / h, shape0[1] / w
        vw, vh = 1600 / 0.25 / dsx, 1000 / 0.25 / dsy
        y0, x0 = max(0, int(cy / dsy - vh / 2)), max(0, int(cx / dsx - vw / 2))
        y1, x1 = min(h, int(y0 + vh) + 1), min(w, int(x0 + vw) + 1)
        tiles = [(ty, tx) for ty in range(y0 // CHUNK, (y1 - 1) // CHUNK + 1)
                 for tx in range(x0 // CHUNK, (x1 - 1) // CHUNK + 1)]
        for c in SWITCH_SEQ:
            ops.append(("tiles", c, k, tiles))
    elif workload == "seq29":
        y0, x0 = cy - 2048, cx - 2048
        for c in range(n_channels):
            ops.append(("region", c, 0, y0, y0 + 4096, x0, x0 + 4096))
    elif workload == "fusion":
        y0, x0 = max(0, cy - 4096), max(0, cx - 4096)
        for by in range(2):
            for bx in range(2):
                ops.append(("fusion", FUSION_CHANNELS, y0 + by * 4096, y0 + (by + 1) * 4096,
                            x0 + bx * 4096, x0 + (bx + 1) * 4096))
    elif workload == "step4":
        h, w = shape0
        batches = [list(range(0, 16)), list(range(16, n_channels))]
        for y0 in range(0, h, 4096):
            for x0 in range(0, w, 4096):
                for b in batches:
                    ops.append(("step4", b, y0, min(h, y0 + 4096), x0, min(w, x0 + 4096)))
    return ops


def execute(op, fmt, reader, extra):
    kind = op[0]
    if kind == "region":
        _, c, k, y0, y1, x0, x1 = op
        return reader.region(c, k, y0, y1, x0, x1).nbytes
    if kind == "tiles":
        _, c, k, tiles = op
        h, w = reader.level_shape(k)
        if fmt == "tif":
            def one(t):
                ty, tx = t
                arr, _ = reader.provider.read_region(c, k, ty * CHUNK, min(h, (ty + 1) * CHUNK),
                                                     tx * CHUNK, min(w, (tx + 1) * CHUNK))
                return arr.nbytes
        else:
            arr = reader.levels[k]

            def one(t):
                ty, tx = t
                return arr[c, ty * CHUNK:min(h, (ty + 1) * CHUNK), tx * CHUNK:min(w, (tx + 1) * CHUNK)].nbytes
        return sum(reader.pool.map(one, tiles))
    if kind == "derived":
        _, k, tiles = op
        h, w = reader.level_shape(k)
        if fmt == "tif":
            from block01.viewer.step1_source import reduce_corrected
            region, stride = extra["region"], 4

            def one(t):
                ty, tx = t
                rect = (ty * CHUNK * stride, min(h, (ty + 1) * CHUNK) * stride,
                        tx * CHUNK * stride, min(w, (tx + 1) * CHUNK) * stride)
                mean, _valid = reduce_corrected(region, rect, stride)
                return np.asarray(mean, np.float32).nbytes
        else:
            arr = extra["stored"][str(k)]

            def one(t):
                ty, tx = t
                return arr[ty * CHUNK:min(h, (ty + 1) * CHUNK), tx * CHUNK:min(w, (tx + 1) * CHUNK)].nbytes
        return sum(reader.pool.map(one, tiles))
    if kind == "fusion":
        _, chans, y0, y1, x0, x1 = op
        if fmt == "tif":
            loader, names = extra["loader"], extra["names"]
            res = list(reader.pool.map(lambda c: loader.read_region(names[c], y0, y1, x0, x1,
                                                                    normalize=False), chans))
            return sum(r.nbytes for r in res)
        return reader.regions(chans, 0, y0, y1, x0, x1).nbytes
    if kind == "step4":
        _, chans, y0, y1, x0, x1 = op
        if fmt == "tif":
            return extra["tiffreader"].read(chans, y0, y1, x0, x1).nbytes
        return reader.regions(chans, 0, y0, y1, x0, x1).nbytes
    raise ValueError(kind)


def cmd_run(args):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    meta = tif_meta(args.tif)
    shape0 = tuple(meta["levels"][0][1:])
    n_channels = meta["levels"][0][0]
    if args.fmt == "tif":
        reader = TifReader(args.tif)
        extra = {}
        if args.workload == "fusion":
            from block01.core.io_loader import OMETIFFLoader
            extra["loader"] = OMETIFFLoader(args.tif)
            extra["names"] = meta["names"]
        if args.workload == "step4":
            from block01.core.quant_sources import TiffTileReader
            extra["tiffreader"] = TiffTileReader(args.tif, threads=THREADS)
    elif args.fmt == "ngff_direct":
        reader = NgffDirectReader(args.ngff)
        extra = {}
    else:
        reader = NgffReader(args.ngff)
        extra = {}
    if args.workload == "derived_l1":
        import zarr
        if args.fmt == "tif":
            extra["region"] = corrected_region(args.corrected, args.roi, args.channel)
            reader.files = lambda: sorted(os.path.join(d, f) for d, _, fs in os.walk(args.corrected) for f in fs)
        else:
            extra["stored"] = zarr.open_group(args.derived, mode="r")
            reader.files = lambda: sorted(os.path.join(d, f) for d, _, fs in os.walk(args.derived) for f in fs)
    level_shapes = [reader.level_shape(k) for k in range(len(meta["levels"]))]
    ops = plan(args.workload, args.tif, shape0, level_shapes, n_channels)
    files = reader.files()
    result = {"fmt": args.fmt, "workload": args.workload, "tif": args.tif, "ngff": args.ngff,
              "threads": THREADS, "blosc_threads": "off (use_threads=False)" if args.fmt == "ngff" else None,
              "n_ops": len(ops), "n_files": len(files), "runs": []}

    def one_run(mode):
        if mode == "cold":
            evict(files)
            res = resident_bytes(files, sample=None if len(files) < 20000 else 5000)
        else:
            res = None
        times, nbytes = [], 0
        t0 = time.perf_counter()
        c0 = os.times()
        for op in ops:
            ts = time.perf_counter()
            nbytes += execute(op, args.fmt, reader, extra)
            times.append((time.perf_counter() - ts) * 1000.0)
        wall = time.perf_counter() - t0
        c1 = os.times()
        t = np.asarray(times)
        return {"mode": mode, "resident_bytes_before": res, "total_s": round(wall, 4),
                "op_median_ms": round(float(np.median(t)), 3), "op_p95_ms": round(float(np.percentile(t, 95)), 3),
                "op_ms": [round(v, 3) for v in times], "bytes_decoded": int(nbytes),
                "cpu_utilisation": round((c1.user + c1.system - c0.user - c0.system) / wall, 2)}

    one_run("warm")                                    # untimed warm-up (handles, imports)
    for _ in range(REPS):
        result["runs"].append(one_run("cold"))
    one_run("warm")
    for _ in range(REPS):
        result["runs"].append(one_run("warm"))
    result["peak_rss_mb"] = round(peak_rss_mb())
    for mode in ("cold", "warm"):
        rs = [r for r in result["runs"] if r["mode"] == mode]
        result[mode] = {"total_s_median": float(np.median([r["total_s"] for r in rs])),
                        "op_median_ms_median": float(np.median([r["op_median_ms"] for r in rs])),
                        "op_p95_ms_median": float(np.median([r["op_p95_ms"] for r in rs])),
                        "cpu_utilisation_median": float(np.median([r["cpu_utilisation"] for r in rs])),
                        "max_resident_before": max((r["resident_bytes_before"] or 0) for r in rs)}
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1)
    print(f"[run] {args.fmt} {args.workload}: cold total {result['cold']['total_s_median']:.3f}s "
          f"(op median {result['cold']['op_median_ms_median']:.1f} ms), warm total "
          f"{result['warm']['total_s_median']:.3f}s, peak RSS {result['peak_rss_mb']} MB, "
          f"resident before cold {result['cold']['max_resident_before']}", flush=True)
    os._exit(0)


def cmd_report(args):
    rows = {}
    for p in args.jsons:
        r = json.load(open(p))
        rows[(r["workload"], r["fmt"])] = r
    print(f"{'workload':15s} {'mode':5s} {'TIFF total s':>12s} {'NGFF total s':>12s} {'TIFF/NGFF':>9s} "
          f"{'TIFF op med':>11s} {'NGFF op med':>11s} {'TIFF p95':>9s} {'NGFF p95':>9s}")
    for wl in WORKLOADS:
        t, n = rows.get((wl, "tif")), rows.get((wl, "ngff"))
        if not t or not n:
            continue
        for mode in ("cold", "warm"):
            a, b = t[mode], n[mode]
            print(f"{wl:15s} {mode:5s} {a['total_s_median']:12.3f} {b['total_s_median']:12.3f} "
                  f"{a['total_s_median'] / b['total_s_median']:9.2f} {a['op_median_ms_median']:11.2f} "
                  f"{b['op_median_ms_median']:11.2f} {a['op_p95_ms_median']:9.2f} {b['op_p95_ms_median']:9.2f}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("--src", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--codec", default="lz4", choices=["lz4", "zstd"])
    p = sub.add_parser("check")
    p.add_argument("ngff")
    p.add_argument("--src", required=True)
    p = sub.add_parser("run")
    p.add_argument("--tif", required=True)
    p.add_argument("--ngff")
    p.add_argument("--fmt", required=True, choices=["tif", "ngff", "ngff_direct"])
    p.add_argument("--workload", required=True, choices=WORKLOADS)
    p.add_argument("--out", required=True)
    p.add_argument("--corrected")
    p.add_argument("--derived")
    p.add_argument("--roi", default="Full WSI")
    p.add_argument("--channel", default="CD3D")
    p = sub.add_parser("derived-ingest")
    p.add_argument("--corrected", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--roi", default="Full WSI")
    p.add_argument("--channel", default="CD3D")
    p = sub.add_parser("report")
    p.add_argument("jsons", nargs="+")
    args = ap.parse_args()
    return {"ingest": cmd_ingest, "derived-ingest": cmd_derived_ingest, "check": cmd_check, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
