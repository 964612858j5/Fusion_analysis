"""
The representative large-image dataset of the v16 plan (§8): a 2x2 MIRRORED
synthetic mosaic of a real OME-TIFF (not imported by the product).

    +--------+--------+
    |   A    | flip x |      each quadrant is the source mirrored about the
    +--------+--------+      shared edge, so every seam is continuous (the
    | flip y | flip xy|      edge row / column simply repeats once) and no
    +--------+--------+      artificial boundary is created

Same as the source: 512 x 512 tiles, LZW, a x4 pyramid with the same number
of levels (each level = floor of the mean of 4x4 / 16x16 level-0 blocks, the
way the source's pyramid was measured to be built), channel names and colours,
PhysicalSizeX/Y. "synthetic" is in the file name and in the OME Name and
Description. The source is only read.

Streaming: one source channel plane (~250 MB) at a time; level 0 is produced
tile row by tile row and compressed on a thread pool by tifffile; levels 1..n
are accumulated while level 0 streams (29 x 7718 x 8107 B ~ 1.8 GB for the
default source), then written. Peak RSS is reported.

    python scripts/make_synthetic_mosaic.py make   [--src S] [--out O] [--workers N]
    python scripts/make_synthetic_mosaic.py verify [--src S] [--out O] [--samples N]

`make` writes <out>.partial and renames it when complete, then writes
<out>.done.json. `verify` checks shape, every pyramid level, tiling /
compression, OME metadata, the product's readers, and sampled regions against
the mirrored source position bit for bit; it writes <out>.verify.json.
`verify --full` also compares EVERY level-0 pixel of every channel with the
mirrored source and every pyramid level with its level-0 recomputation (one
channel plane at a time; the source's borders are background, so the seams of
the full mosaic lie in zeros and sampled windows alone are weak evidence).
`--crop H W C [--crop-origin Y X]` (both commands) uses only an H x W window
of the first C channels, for a quick smoke run; pick an origin inside tissue.
"""

import argparse
import json
import os
import resource
import sys
import time
import xml.etree.ElementTree as ET

import numpy as np

HOME = os.path.expanduser("~")
DEFAULT_SRC = os.path.join(HOME, "fusion_data", "cropped_region.ome.tif")
DEFAULT_OUT = os.path.join(HOME, "fusionflux", "synthetic", "synthetic_2x2_mirror.ome.tif")
PROTECTED = os.path.realpath(os.path.join(HOME, "fusion_data"))
OME_NS = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
TILE = 512
FACTOR = 4


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def mirror_index(i, n):
    """Mosaic index -> source index along one axis of length n (mosaic 2n)."""
    i = np.asarray(i, dtype=np.int64)
    return np.where(i < n, i, 2 * n - 1 - i)


def source_meta(tifffile, path, crop, origin=(0, 0)):
    with tifffile.TiffFile(path) as tif:
        s = tif.series[0]
        kf = s.levels[0].keyframe
        root = ET.fromstring(tif.ome_metadata)
        pix = root.find(".//ome:Pixels", OME_NS)
        chans = root.findall(".//ome:Channel", OME_NS)
        meta = {
            "axes": str(s.axes),
            "shape": [int(v) for v in s.shape],
            "dtype": str(s.dtype),
            "levels": [[int(v) for v in lv.shape] for lv in s.levels],
            "tile": [int(kf.tilelength), int(kf.tilewidth)],
            "compression": int(kf.compression),
            "predictor": int(kf.predictor),
            "names": [c.get("Name") for c in chans],
            "colors": [c.get("Color") for c in chans],
            "psx": float(pix.get("PhysicalSizeX")),
            "psy": float(pix.get("PhysicalSizeY")),
            "psx_unit": pix.get("PhysicalSizeXUnit"),
            "psy_unit": pix.get("PhysicalSizeYUnit"),
        }
    if meta["axes"] != "CYX" or meta["dtype"] != "uint8" or meta["tile"] != [TILE, TILE]:
        raise SystemExit(f"unexpected source layout: {meta['axes']} {meta['dtype']} {meta['tile']}")
    meta["origin"] = [0, 0]
    if crop:
        h, w, c = crop
        meta["origin"] = [int(origin[0]), int(origin[1])]
        if (meta["origin"][0] + h > meta["shape"][1] or meta["origin"][1] + w > meta["shape"][2]
                or c > meta["shape"][0]):
            raise SystemExit("the crop window leaves the source")
        meta["shape"] = [c, h, w]
        meta["names"], meta["colors"] = meta["names"][:c], meta["colors"][:c]
    return meta


def mosaic_shapes(c, h, w, nlev, factor=FACTOR):
    shapes = [(c, 2 * h, 2 * w)]
    for k in range(1, nlev):
        shapes.append((c, (2 * h) // factor ** k, (2 * w) // factor ** k))
    return shapes


def pyramid_levels(oh, ow, factor):
    """Block A9-M (C5): the level count of a `factor` pyramid over an
    (oh, ow) level 0 -- every level whose longest side is at least half a
    tile (a x2 pyramid over the 2x2 mosaic: 7 levels, 30874 ... 482)."""
    n = 1
    while max(oh, ow) // factor ** n >= TILE // 2:
        n += 1
    return n


def block_floor_mean(a, f):
    """floor(mean) of every complete f x f block of a 2-D uint8 array."""
    h, w = (a.shape[0] // f) * f, (a.shape[1] // f) * f
    s = a[:h, :w].reshape(h // f, f, w // f, f).sum(axis=(1, 3), dtype=np.uint32)
    return (s // (f * f)).astype(np.uint8)


def check_out_path(out, src, force):
    real = os.path.realpath(out)
    if real == PROTECTED or real.startswith(PROTECTED + os.sep):
        raise SystemExit(f"refusing to write under {PROTECTED}")
    if real == os.path.realpath(src):
        raise SystemExit("output equals the source")
    if "synthetic" not in os.path.basename(out):
        raise SystemExit("the output file name must contain 'synthetic'")
    if os.path.exists(out) and not force:
        raise SystemExit(f"{out} exists (use --force)")


def cmd_make(args):
    import tifffile

    check_out_path(args.out, args.src, args.force)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    meta = source_meta(tifffile, args.src, args.crop, args.crop_origin)
    C, H, W = meta["shape"]
    oy, ox = meta["origin"]
    nlev = len(meta["levels"])
    shapes = mosaic_shapes(C, H, W, nlev)
    OH, OW = shapes[0][1:]
    lower = [np.zeros(s, np.uint8) for s in shapes[1:]]   # levels 1..n-1
    print(f"[make] source {C}x{H}x{W} -> mosaic levels {shapes}", flush=True)
    stats = {"channel_seconds": []}
    t_all = time.perf_counter()

    col_src = mirror_index(np.arange(OW), W)
    tif_src = tifffile.TiffFile(args.src)
    pages = tif_src.series[0].levels[0].pages

    def tiles():
        for c in range(C):
            t0 = time.perf_counter()
            plane = pages[c].asarray(maxworkers=args.workers)[oy:oy + H, ox:ox + W]
            for y0 in range(0, OH, TILE):
                y1 = min(y0 + TILE, OH)
                strip = plane[mirror_index(np.arange(y0, y1), H)][:, col_src]
                for k, lv in enumerate(lower, start=1):
                    f = FACTOR ** k
                    red = block_floor_mean(strip, f)
                    lv[c, y0 // f:y0 // f + red.shape[0], :red.shape[1]] = red
                if y1 - y0 < TILE:
                    strip = np.pad(strip, ((0, TILE - (y1 - y0)), (0, 0)))
                for x0 in range(0, OW, TILE):
                    t = strip[:, x0:x0 + TILE]
                    if t.shape[1] < TILE:
                        t = np.pad(t, ((0, 0), (0, TILE - t.shape[1])))
                    yield np.ascontiguousarray(t)
            del plane
            dt = time.perf_counter() - t0
            stats["channel_seconds"].append(round(dt, 1))
            print(f"[make] level 0 channel {c + 1}/{C} {meta['names'][c]}: {dt:.1f}s, "
                  f"peak RSS {peak_rss_mb():.0f} MB", flush=True)

    description = (
        f"SYNTHETIC 2x2 mirrored mosaic of {os.path.basename(args.src)} "
        f"(window {H}x{W} at y={oy} x={ox}, {C} channels). Quadrants: source, flipped in x, "
        "flipped in y, flipped in x and y. Repeated content: for IO / "
        "boundedness / identity / invariance tests only, not biology. "
        "Generated by Fusion_analysis/scripts/make_synthetic_mosaic.py.")
    ome = {
        "axes": "CYX",
        "Name": "synthetic_2x2_mirror",
        "Description": description,
        "PhysicalSizeX": meta["psx"], "PhysicalSizeXUnit": meta["psx_unit"],
        "PhysicalSizeY": meta["psy"], "PhysicalSizeYUnit": meta["psy_unit"],
        "Channel": {"Name": meta["names"], "Color": [int(v) for v in meta["colors"]]},
    }
    common = dict(tile=(TILE, TILE), compression="lzw", photometric="minisblack",
                  resolutionunit="CENTIMETER", maxworkers=args.workers)
    res0 = (1e4 / meta["psx"], 1e4 / meta["psy"])
    tmp = args.out + ".partial"
    with tifffile.TiffWriter(tmp, bigtiff=True, ome=True) as tw:
        tw.write(tiles(), shape=shapes[0], dtype="uint8", subifds=nlev - 1,
                 resolution=res0, metadata=ome, software="FusionFlux synthetic mosaic",
                 **common)
        tif_src.close()
        for k, lv in enumerate(lower, start=1):
            t0 = time.perf_counter()
            sy, sx = OH / lv.shape[1], OW / lv.shape[2]
            tw.write(lv, subfiletype=1, resolution=(res0[0] / sx, res0[1] / sy),
                     metadata=None, **common)
            print(f"[make] level {k} {lv.shape}: {time.perf_counter() - t0:.1f}s", flush=True)
    os.replace(tmp, args.out)
    stats.update({
        "source": args.src, "out": args.out, "crop": args.crop, "crop_origin": meta["origin"], "levels": shapes,
        "seconds": round(time.perf_counter() - t_all, 1),
        "bytes": os.path.getsize(args.out), "source_bytes": os.path.getsize(args.src),
        "peak_rss_mb": round(peak_rss_mb()), "workers": args.workers,
    })
    with open(args.out + ".done.json", "w") as fh:
        json.dump(stats, fh, indent=1)
    print(f"[make] done: {stats['bytes'] / 1e9:.2f} GB in {stats['seconds']}s, "
          f"peak RSS {stats['peak_rss_mb']} MB", flush=True)


# ── verify ───────────────────────────────────────────────────────────────


def expected_region(src_level0, c, y0, y1, x0, x1, H, W, origin=(0, 0)):
    """What the mosaic must hold at [c, y0:y1, x0:x1], read from the source."""
    ry = mirror_index(np.arange(y0, y1), H) + origin[0]
    rx = mirror_index(np.arange(x0, x1), W) + origin[1]
    block = np.asarray(src_level0[c, ry.min():ry.max() + 1, rx.min():rx.max() + 1])
    return block[np.ix_(ry - ry.min(), rx - rx.min())]


def full_check(args, meta, shapes, tif, src_tif, check):
    """Every pixel: level 0 per quadrant against the source window, levels
    1..n against floor(mean) of the whole level-0 plane."""
    C, H, W = meta["shape"]
    oy, ox = meta["origin"]
    src_pages = src_tif.series[0].levels[0].pages
    out_levels = tif.series[0].levels
    bad0, badk, nz = [], [], 0
    for c in range(C):
        t0 = time.perf_counter()
        a = src_pages[c].asarray(maxworkers=args.workers)[oy:oy + H, ox:ox + W]
        m = out_levels[0].pages[c].asarray(maxworkers=args.workers)
        nz += int(np.count_nonzero(a))
        quads = {"top-left": (m[:H, :W], a), "top-right": (m[:H, W:], a[:, ::-1]),
                 "bottom-left": (m[H:, :W], a[::-1, :]), "bottom-right": (m[H:, W:], a[::-1, ::-1])}
        bad0 += [(c, q) for q, (g, e) in quads.items() if not np.array_equal(g, e)]
        del a, quads
        for k in range(1, len(shapes)):
            lv = out_levels[k].pages[c].asarray(maxworkers=args.workers)
            if not np.array_equal(lv, block_floor_mean(m, FACTOR ** k)):
                badk.append((c, k))
        del m
        print(f"[verify] full: channel {c + 1}/{C} {time.perf_counter() - t0:.1f}s, "
              f"peak RSS {peak_rss_mb():.0f} MB", flush=True)
    check(f"FULL level 0: all {C} channels x 4 quadrants bitwise equal to the (mirrored) source",
          not bad0, {"source_nonzero_pixels": nz, "mismatching": bad0[:8]})
    check(f"FULL levels 1..{len(shapes) - 1}: every pixel = floor(mean) of level 0", not badk, badk[:8] or None)


def cmd_verify(args):
    import tifffile
    import zarr

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    rng = np.random.default_rng(args.seed)
    meta = source_meta(tifffile, args.src, args.crop, args.crop_origin)
    C, H, W = meta["shape"]
    factor = int(getattr(args, "pyramid_factor", None) or FACTOR)
    nlev = (len(meta["levels"]) if factor == FACTOR
            else pyramid_levels(2 * H, 2 * W, factor))
    shapes = mosaic_shapes(C, H, W, nlev, factor)
    OH, OW = shapes[0][1:]
    report = {"out": args.out, "checks": {}, "failures": [], "info": {},
              "pyramid_factor": factor, "levels": nlev}

    def check(name, ok, detail=None):
        report["checks"][name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            report["failures"].append(name)
        print(f"[verify] {'OK  ' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail is not None else ""),
              flush=True)

    def info(name, detail):
        report["info"][name] = detail
        print(f"[verify] INFO {name}  {detail}", flush=True)

    check("file name contains 'synthetic'", "synthetic" in os.path.basename(args.out))
    tif = tifffile.TiffFile(args.out)
    s = tif.series[0]
    check("one series, OME", len(tif.series) == 1 and tif.is_ome, len(tif.series))
    check("axes / dtype", str(s.axes) == "CYX" and str(s.dtype) == "uint8", f"{s.axes} {s.dtype}")
    got_levels = [tuple(int(v) for v in lv.shape) for lv in s.levels]
    check("pyramid level shapes", got_levels == [tuple(v) for v in shapes], got_levels)
    for k, lv in enumerate(s.levels):
        pgs = [p.aspage() if hasattr(p, "aspage") else p for p in lv.pages]
        bad = [i for i, p in enumerate(pgs)
               if (p.tilelength, p.tilewidth) != (TILE, TILE)
               or int(p.compression) != meta["compression"] or int(p.predictor) != meta["predictor"]]
        check(f"level {k}: {len(lv.pages)} pages tiled {TILE}, LZW, predictor as source",
              len(lv.pages) == C and not bad, bad[:5] or None)
    root = ET.fromstring(tif.ome_metadata)
    pix = root.find(".//ome:Pixels", OME_NS)
    chans = root.findall(".//ome:Channel", OME_NS)
    check("OME channel names", [c.get("Name") for c in chans] == meta["names"])
    check("OME channel colours", [c.get("Color") for c in chans] == meta["colors"])
    check("OME PhysicalSizeX/Y and unit",
          float(pix.get("PhysicalSizeX")) == meta["psx"] and float(pix.get("PhysicalSizeY")) == meta["psy"]
          and pix.get("PhysicalSizeXUnit") == meta["psx_unit"],
          (pix.get("PhysicalSizeX"), pix.get("PhysicalSizeY"), pix.get("PhysicalSizeXUnit")))
    desc = root.find(".//ome:Image/ome:Description", OME_NS)
    img = root.find(".//ome:Image", OME_NS)
    check("OME Name / Description say synthetic",
          desc is not None and "SYNTHETIC" in desc.text and "synthetic" in img.get("Name", ""))
    check("OME SizeX/SizeY", (int(pix.get("SizeX")), int(pix.get("SizeY")), int(pix.get("SizeC"))) == (OW, OH, C))

    src_tif = tifffile.TiffFile(args.src)
    src0 = zarr.open(src_tif.series[0].aszarr(level=0), mode="r")
    out_z = [zarr.open(s.aszarr(level=k), mode="r") for k in range(nlev)]

    # Level 0: bitwise against the mirrored source position.
    windows = []
    for yc in (0, H - 1, H, OH - 1):          # corners and both seams
        for xc in (0, W - 1, W, OW - 1):
            windows.append((yc, xc, 300))
    for _ in range(args.samples):
        sz = int(rng.integers(64, 900))
        windows.append((int(rng.integers(0, OH)), int(rng.integers(0, OW)), sz))
    n_px, n_nz, bad_w = 0, 0, []
    for i, (yc, xc, sz) in enumerate(windows):
        y0, x0 = max(0, yc - sz // 2), max(0, xc - sz // 2)
        y1, x1 = min(OH, y0 + sz), min(OW, x0 + sz)
        chs = range(C) if i < 16 else rng.choice(C, size=min(3, C), replace=False)
        for c in chs:
            got = np.asarray(out_z[0][int(c), y0:y1, x0:x1])
            exp = expected_region(src0, int(c), y0, y1, x0, x1, H, W, meta["origin"])
            n_px += got.size
            n_nz += int(np.count_nonzero(exp))
            if not np.array_equal(got, exp):
                bad_w.append((int(c), y0, y1, x0, x1))
    check(f"level 0: {len(windows)} windows (16 corner/seam windows x all channels) "
          "bitwise equal to the mirrored source", not bad_w,
          {"pixels": n_px, "nonzero_pixels": n_nz, "mismatching": bad_w[:5]})

    c = 0
    row_a, row_b = np.asarray(out_z[0][c, H - 1, :]), np.asarray(out_z[0][c, H, :])
    col_a, col_b = np.asarray(out_z[0][c, :, W - 1]), np.asarray(out_z[0][c, :, W])
    check("seams continuous (edge row/column mirrored)",
          np.array_equal(row_a, row_b) and np.array_equal(col_a, col_b))

    # Levels 1..n: bitwise against floor(mean) of the mosaic's own level-0 blocks.
    for k in range(1, nlev):
        f = factor ** k
        lh, lw = shapes[k][1:]
        bad, nwin = [], 0
        spots = [(0, 0), (lh // 2 - 40, lw // 2 - 40), (lh - 80, lw - 80), (H // f - 40, W // f - 40)]
        spots += [(int(rng.integers(0, lh - 80)), int(rng.integers(0, lw - 80))) for _ in range(12)]
        for (ly, lx) in spots:
            ly, lx = max(0, ly), max(0, lx)
            ly1, lx1 = min(lh, ly + 80), min(lw, lx + 80)
            for c in rng.choice(C, size=min(3, C), replace=False):
                got = np.asarray(out_z[k][int(c), ly:ly1, lx:lx1])
                base = np.asarray(out_z[0][int(c), ly * f:ly1 * f, lx * f:lx1 * f])
                nwin += 1
                if not np.array_equal(got, block_floor_mean(base, f)):
                    bad.append((int(c), ly, lx))
        check(f"level {k}: {nwin} windows = floor(mean {f}x{f}) of level 0", not bad, bad[:5] or None)
        if not args.crop and factor == FACTOR:
            # Top-left quadrant against the SOURCE's own pyramid level. Not a
            # pass / fail check: the source's pyramid was built by another
            # tool. Measured on cropped_region: level 1 ~0.99998 equal (the
            # same 4x4 floor-mean rule); level 2 ~0.79, and no block-mean,
            # nearest or cv2 resampling of its level 0 / 1 reproduces it.
            sk = zarr.open(src_tif.series[0].aszarr(level=k), mode="r")
            hh, ww = min(sk.shape[1], lh, 1500), min(sk.shape[2], lw, 1500)
            eq = float(np.mean(np.asarray(sk[0, :hh, :ww]) == np.asarray(out_z[k][0, :hh, :ww])))
            info(f"level {k}: top-left quadrant equal to the source's own level {k}, channel 0 "
                 "(fraction)", round(eq, 6))
    if args.full:
        full_check(args, meta, shapes, tif, src_tif, check)
    tif.close()

    # The product's readers (read-only).
    try:
        from block01.core.io_loader import OMETIFFLoader
        from block01.viewer.raw_tile_provider import RawTileProvider
        from block01.core.quant_sources import TiffTileReader, _slide_channels
        yc, xc = H - 200, W - 200          # a window across both seams
        exp = expected_region(src0, 1, yc, yc + 400, xc, xc + 400, H, W, meta["origin"])
        ld = OMETIFFLoader(args.out)
        got = ld.read_region(meta["names"][1], yc, yc + 400, xc, xc + 400, normalize=False)
        check("product OMETIFFLoader: shape, channels, raw region",
              ld.shape == (OH, OW) and ld.channel_names() == meta["names"]
              and np.array_equal(got.astype(np.uint8), exp))
        rp = RawTileProvider(args.out)
        arr, off = rp.read_region(meta["names"][1], 0, yc, yc + 400, xc, xc + 400)
        check("product RawTileProvider: level shapes, channels, raw region",
              [rp.level_shape(k) for k in range(rp.num_levels)] == [tuple(v[1:]) for v in shapes]
              and rp.channel_names == meta["names"] and np.array_equal(arr, exp),
              [rp.level_shape(k) for k in range(rp.num_levels)])
        rp.close()
        names, shp = _slide_channels(args.out)
        tr = TiffTileReader(args.out, threads=4)
        got = tr.read([1], yc, yc + 400, xc, xc + 400)
        got = np.asarray(got)[0] if np.asarray(got).ndim == 3 else np.asarray(got)
        check("product Step4 TiffTileReader (fast tile path) + _slide_channels",
              tr.mode == "tiff_tiles" and names == meta["names"] and tuple(shp) == (OH, OW)
              and np.array_equal(got, exp), tr.mode)
        tr.close()
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        import traceback
        check("product readers", False, traceback.format_exc(limit=-3))

    src_tif.close()
    report["peak_rss_mb"] = round(peak_rss_mb())
    with open(args.out + ".verify.json", "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(f"[verify] {'PASS' if not report['failures'] else 'FAIL'}: "
          f"{len(report['checks']) - len(report['failures'])}/{len(report['checks'])} checks", flush=True)
    return 0 if not report["failures"] else 1


def cmd_repyramid(args):
    """Block A9-M (C5): the same mosaic with a `--pyramid-factor` pyramid.

    Level 0 is copied tile for tile from the existing mosaic (`--mosaic`);
    level k is floor(mean) of every complete factor**k block of level 0 --
    the rule `verify` checks. One channel plane in memory at a time (the
    lower levels of a x2 pyramid are a third of level 0 together: holding
    them, as `make` does, would not fit on the dev machine)."""
    import tifffile

    factor = int(args.pyramid_factor or 2)
    check_out_path(args.out, args.mosaic, args.force)
    meta = source_meta(tifffile, args.mosaic, None)
    C, OH, OW = meta["shape"]
    nlev = pyramid_levels(OH, OW, factor)
    shapes = [(C, OH, OW)] + [(C, OH // factor ** k, OW // factor ** k) for k in range(1, nlev)]
    print(f"[repyramid] {args.mosaic} -> x{factor}, {nlev} levels {shapes}", flush=True)
    src = tifffile.TiffFile(args.mosaic)
    pages = src.series[0].levels[0].pages

    def tiles_of(plane):
        h, w = plane.shape
        for y0 in range(0, h, TILE):
            strip = plane[y0:y0 + TILE]
            if strip.shape[0] < TILE:
                strip = np.pad(strip, ((0, TILE - strip.shape[0]), (0, 0)))
            for x0 in range(0, w, TILE):
                t = strip[:, x0:x0 + TILE]
                if t.shape[1] < TILE:
                    t = np.pad(t, ((0, 0), (0, TILE - t.shape[1])))
                yield np.ascontiguousarray(t)

    def level_tiles(k):
        f = factor ** k
        for c in range(C):
            t0 = time.perf_counter()
            plane = pages[c].asarray(maxworkers=args.workers)
            if k:
                plane = block_floor_mean(plane, f)[:shapes[k][1], :shapes[k][2]]
            yield from tiles_of(plane)
            del plane
            print(f"[repyramid] level {k} channel {c + 1}/{C}: {time.perf_counter() - t0:.1f}s, "
                  f"peak RSS {peak_rss_mb():.0f} MB", flush=True)

    description = (
        f"SYNTHETIC 2x2 mirrored mosaic, x{factor} pyramid ({nlev} levels) rebuilt from "
        f"{os.path.basename(args.mosaic)} (level 0 copied). Repeated content: for IO / "
        "boundedness / viewer tests only, not biology. Generated by "
        "Fusion_analysis/scripts/make_synthetic_mosaic.py repyramid.")
    ome = {"axes": "CYX", "Name": "synthetic_2x2_mirror", "Description": description,
           "PhysicalSizeX": meta["psx"], "PhysicalSizeXUnit": meta["psx_unit"],
           "PhysicalSizeY": meta["psy"], "PhysicalSizeYUnit": meta["psy_unit"],
           "Channel": {"Name": meta["names"], "Color": [int(v) for v in meta["colors"]]}}
    common = dict(tile=(TILE, TILE), compression="lzw", photometric="minisblack",
                  resolutionunit="CENTIMETER", maxworkers=args.workers)
    res0 = (1e4 / meta["psx"], 1e4 / meta["psy"])
    tmp = args.out + ".partial"
    t_all = time.perf_counter()
    with tifffile.TiffWriter(tmp, bigtiff=True, ome=True) as tw:
        tw.write(level_tiles(0), shape=shapes[0], dtype="uint8", subifds=nlev - 1,
                 resolution=res0, metadata=ome, software="FusionFlux synthetic mosaic",
                 **common)
        for k in range(1, nlev):
            sy, sx = OH / shapes[k][1], OW / shapes[k][2]
            tw.write(level_tiles(k), shape=shapes[k], dtype="uint8", subfiletype=1,
                     resolution=(res0[0] / sx, res0[1] / sy), metadata=None, **common)
    src.close()
    os.replace(tmp, args.out)
    done = {"mosaic": args.mosaic, "out": args.out, "pyramid_factor": factor,
            "levels": nlev, "shapes": shapes, "seconds": round(time.perf_counter() - t_all, 1),
            "bytes": os.path.getsize(args.out), "peak_rss_mb": round(peak_rss_mb())}
    with open(args.out + ".done.json", "w") as fh:
        json.dump(done, fh, indent=1)
    print(f"[repyramid] done: {done}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["make", "verify", "repyramid"])
    ap.add_argument("--mosaic", default=DEFAULT_OUT,
                    help="repyramid: the existing mosaic whose level 0 is copied")
    ap.add_argument("--pyramid-factor", type=int, default=None,
                    help="repyramid / verify: the pyramid's per-level factor (default 4)")
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--samples", type=int, default=60)
    ap.add_argument("--seed", type=int, default=16)
    ap.add_argument("--crop", type=int, nargs=3, metavar=("H", "W", "C"))
    ap.add_argument("--crop-origin", type=int, nargs=2, metavar=("Y", "X"), default=(0, 0))
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.command == "make":
        cmd_make(args)
        return 0
    if args.command == "repyramid":
        cmd_repyramid(args)
        return 0
    return cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
