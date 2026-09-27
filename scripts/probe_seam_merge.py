"""
Block N3a: probe Step2's tile seams on real data (not imported by the product).

1. Runs the REAL Step2 worker (same method, parameters and tile grid as a
   finished run) on a COPY of the run's fused zarr, writing only into
   --output-dir; wraps the worker instance's `_segment_tile` to save every
   tile's own segmentation (window labels) as it is produced. The product
   code is not changed.
2. Offline, with `core/seam_merge.py`: candidates (cells not touching an
   internal window edge), the seam candidates (inside another tile's window),
   every cross-tile overlapping pair's overlap / smaller area, IoU, centroid
   distance and ownership margins, the overlap graph's components and the
   decision at a probe `tau`.
3. Paints the reconciled region and compares it with the product's merged
   mask from the same run: empty labels, fragmented / tiny cells near the
   seams against control bands.

    python scripts/probe_seam_merge.py --run <finished run folder> [--tau 0.5]
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
os.environ.setdefault("KERAS_BACKEND", "tensorflow")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

RUN = ("~/fusion_data/test1/rois/full_wsi_20260927_121444_6bad/step2/segmentation_runs/"
       "seg_20260927_124316_stardist_nuclei_expansion")


def _git():
    try:
        return subprocess.run(["git", "-C", HERE, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


def run_step2(run, work, tiles_dir):
    """The real worker on a copy of the fused zarr; every tile's labels saved."""
    from PyQt5 import QtWidgets
    from block01.utils.segmentation_config import normalize_segmentation_config
    from block01.workers.segment_merge_worker import SegmentMergeWorker
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    meta = json.load(open(os.path.join(run, "segmentation_meta.json")))
    region = meta["rois"][0] if meta.get("rois") else meta
    fused = region.get("fused_zarr_path") or meta.get("fused_zarr_path")
    copy = os.path.join(work, "fused.zarr")
    if not os.path.exists(copy):
        shutil.copytree(fused, copy)
    cfg = normalize_segmentation_config(json.load(open(os.path.join(run, "run_segmentation_params.json"))))
    rows, cols = meta.get("tile_grid") or json.load(open(glob.glob(
        os.path.join(run, "segmentation_meta_*.json"))[0])).get("tile_grid")
    overlap = int((meta.get("tile_strategy") or {}).get("suggested_overlap") or 200)
    worker = SegmentMergeWorker(copy, seg_config=cfg, n_rows=rows, n_cols=cols,
                                overlap_px=overlap, output_dir=os.path.join(work, "proj"))
    real = worker._segment_tile
    count = {"n": 0}

    def recording(*a, **k):
        res = real(*a, **k)
        mask = res["mask"] if isinstance(res, dict) else res
        np.save(os.path.join(tiles_dir, f"tile_{count['n']:03d}.npy"), np.asarray(mask, np.uint32))
        count["n"] += 1
        return res
    worker._segment_tile = recording
    errors = []
    worker.error.connect(lambda m: errors.append(m))
    t0 = time.perf_counter()
    worker.run()
    if errors:
        raise SystemExit("Step2 failed:\n" + "\n".join(errors))
    return worker, rows, cols, overlap, time.perf_counter() - t0


def seam_stats(lab, own_edges_y, own_edges_x, d=150):
    from scipy import ndimage as ndi
    H, W = lab.shape
    n = int(lab.max())
    cnt = np.bincount(lab.ravel(), minlength=n + 1)
    objs = ndi.find_objects(lab)
    med = float(np.median(cnt[1:][cnt[1:] > 0]))

    def band(ly, lx):
        near = frag = tiny = 0
        for i, sl in enumerate(objs):
            if sl is None:
                continue
            y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
            if any(y0 - d <= L <= y1 + d for L in ly) or any(x0 - d <= L <= x1 + d for L in lx):
                near += 1
                frag += ndi.label(lab[sl] == i + 1)[1] > 1
                tiny += cnt[i + 1] < 0.25 * med
        return {"cells": near, "fragmented": int(frag), "tiny": int(tiny)}
    return {"labels": n, "empty_labels": int((cnt[1:] == 0).sum()), "median_area": med,
            "near_seams": band(own_edges_y, own_edges_x),
            "control_band": band([y + 1000 for y in own_edges_y if y + 1000 < H],
                                 [x + 1000 for x in own_edges_x if x + 1000 < W])}


def hist(values, edges):
    h, _ = np.histogram(values, bins=edges)
    return {f"[{edges[i]:g},{edges[i + 1]:g})": int(h[i]) for i in range(len(h))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=RUN)
    ap.add_argument("--tau", type=float, default=0.5)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--reuse", action="store_true", help="skip Step2 if the tiles exist")
    args = ap.parse_args()
    import zarr
    from block01.core import seam_merge as sm
    run = os.path.expanduser(args.run)
    out = os.path.expanduser(args.output_dir) if args.output_dir else os.path.expanduser(
        f"~/fusionflux/bench_step2/test1_tophat/{datetime.date.today()}_{_git()}_seam")
    work = os.path.join(out, "work")
    tiles_dir = os.path.join(work, "tiles")
    os.makedirs(tiles_dir, exist_ok=True)
    report = {"git": _git(), "run": run, "tau_probe": args.tau, "output_dir": out}

    if args.reuse and glob.glob(os.path.join(tiles_dir, "tile_*.npy")):
        info = json.load(open(os.path.join(out, "step2_run.json")))
    else:
        worker, rows, cols, overlap, secs = run_step2(run, work, tiles_dir)
        prof = json.load(open(os.path.join(worker.output_dir, "step2_profile.json")))
        tm = prof["tile_metadata"]
        info = {"rows": rows, "cols": cols, "overlap": overlap, "seconds": secs,
                "output_dir": worker.output_dir,
                "tiles": [{"own": tm[k]["bbox_local"], "shape": tm[k]["tile_shape"]}
                          for k in sorted(tm, key=lambda s: int(s.rsplit(":", 1)[-1])
                                          if s.rsplit(":", 1)[-1].isdigit() else s)]}
        json.dump(info, open(os.path.join(out, "step2_run.json"), "w"), indent=1)
        del worker
    report["step2"] = {k: info[k] for k in ("rows", "cols", "overlap", "seconds")}
    merged_meta = json.load(open(os.path.join(info["output_dir"], "segmentation_meta.json")))
    product = zarr.open(merged_meta["zarr_path"], mode="r")[:]
    H, W = product.shape

    tiles = []
    for i, t in enumerate(info["tiles"]):
        oy0, oy1, ox0, ox1 = (int(v) for v in t["own"])
        ov = info["overlap"]
        read = (max(0, oy0 - ov), min(H, oy1 + ov), max(0, ox0 - ov), min(W, ox1 + ov))
        assert [read[1] - read[0], read[3] - read[2]] == list(t["shape"]), (i, read, t)
        tiles.append(sm.Tile(i, read, (oy0, oy1, ox0, ox1)))
    report["tiles"] = [{"read": list(t.read), "own": list(t.own)} for t in tiles]

    # candidates
    cands, touching, interior = [], 0, 0
    interior_cands = []
    for t in tiles:
        local = np.load(os.path.join(tiles_dir, f"tile_{t.index:03d}.npy"))
        cs, n_touch = sm.extract(t, local, (H, W))
        touching += n_touch
        for c in cs:
            if sm.is_seam(c, tiles):
                cands.append(c)
            else:
                interior += 1
                interior_cands.append(c)
        del local
    report["candidates"] = {"seam": len(cands), "interior": interior,
                            "touching_internal_edge_dropped": touching,
                            "interior_not_owned": sum(1 for c in interior_cands if not c.owned)}

    # pairs and their distributions
    P = sm.pairs(cands)
    frac = np.array([p[3] for p in P])
    iou = np.array([p[4] for p in P])
    dist = np.array([p[5] for p in P])
    edges = [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, 1.0001]
    report["pairs"] = {
        "overlapping_pairs": len(P),
        "overlap_over_min_area": hist(frac, edges),
        "iou": hist(iou, edges),
        "centroid_distance_px": hist(dist, [0, 1, 2, 3, 5, 8, 12, 20, 40, 1e9]),
        "near_threshold_0.3_0.7": int(((frac >= .3) & (frac < .7)).sum()),
    }
    with open(os.path.join(out, "pairs.csv"), "w") as f:
        f.write("tile_a,label_a,tile_b,label_b,area_a,area_b,inter,frac_min,iou,centroid_dist,"
                "margin_a,margin_b,owned_a,owned_b,cy_a,cx_a\n")
        for i, j, inter, fr, io, d in P:
            a, b = cands[i], cands[j]
            f.write(f"{a.tile},{a.label},{b.tile},{b.label},{a.area},{b.area},{inter},{fr:.4f},"
                    f"{io:.4f},{d:.2f},{a.margin:.1f},{b.margin:.1f},{int(a.owned)},{int(b.owned)},"
                    f"{a.cy:.1f},{a.cx:.1f}\n")

    # decision at the probe tau
    dec = sm.resolve(cands, tiles, args.tau)
    report["decision"] = dict(dec.stats)
    today_kept = sum(1 for c in cands if c.owned)
    report["decision"]["seam_candidates_kept_today"] = today_kept
    report["decision"]["seam_candidates_kept_new"] = len(dec.kept)
    for t in (0.3, 0.4, 0.6, 0.7):
        report.setdefault("decision_at_other_tau", {})[str(t)] = sm.resolve(cands, tiles, t).stats

    # paint: interior cells as today (owned), then the reconciled seam cells
    all_c = interior_cands + cands
    ey = sorted({t.own[0] for t in tiles if t.own[0] > 0})
    ex = sorted({t.own[2] for t in tiles if t.own[2] > 0})
    base = len(interior_cands)
    variants = {}
    for tau in (0.3, 0.5, 0.7):
        d = sm.resolve(cands, tiles, tau)
        order = [k for k in range(base) if all_c[k].owned] + [base + k for k in d.kept]
        order.sort(key=lambda k: (-all_c[k].margin, all_c[k].tile, all_c[k].label))
        for f in (0.0, 0.5, 0.7, 0.9):
            img, clip = sm.paint(all_c, order, (H, W), min_free_fraction=f)
            st = seam_stats(img, ey, ex)
            variants[f"tau={tau} min_free={f}"] = {
                "cells": int(img.max()), "dropped_whole": int(sum(1 for k, v in clip.items()
                                                                  if v == all_c[k].area)),
                "cells_with_clipped_pixels": int(sum(1 for k, v in clip.items()
                                                     if 0 < v < all_c[k].area)),
                "near_seams": st["near_seams"], "control_band": st["control_band"],
                "empty_labels": st["empty_labels"]}
            print(f"tau={tau} min_free={f}: {variants[f'tau={tau} min_free={f}']}", flush=True)
            del img
    report["variants"] = variants
    base = len(interior_cands)
    kept = [k for k in range(base) if all_c[k].owned] + [base + k for k in dec.kept]
    kept.sort(key=lambda k: (-all_c[k].margin, all_c[k].tile, all_c[k].label))
    new, clipped = sm.paint(all_c, kept, (H, W))
    report["painted"] = {"cells": int(new.max()),
                         "cells_with_clipped_pixels": int(sum(1 for v in clipped.values() if v)),
                         "clipped_pixels": int(sum(clipped.values())),
                         "dropped_fully_covered": int(sum(1 for k, v in clipped.items()
                                                          if v == all_c[k].area))}
    ey = sorted({t.own[0] for t in tiles if t.own[0] > 0})
    ex = sorted({t.own[2] for t in tiles if t.own[2] > 0})
    report["product_merge"] = seam_stats(product, ey, ex)
    report["reconciled"] = seam_stats(new, ey, ex)
    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps({k: report[k] for k in ("candidates", "pairs", "decision", "painted",
                                             "product_merge", "reconciled")}, indent=1))


if __name__ == "__main__":
    main()
