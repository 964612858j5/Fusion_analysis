"""
Block S4-3 acceptance on real data and the memory gate (not imported by the
product). Read-only on the project; outputs outside the repo.

  real    the PRODUCT path on the tophat workspace's run, 3 markers (two raw,
          one Step0-corrected) x 3 regions x median / p90 / p95 / Gini:
            * every object against numpy (median, percentile 'linear') and
              the Gini formula, from the whole arrays;
            * perimeter_crofton against skimage regionprops, every object;
            * the fast statistics unchanged: the columns shared with S4-1's
              product CSV equal it character by character;
            * a run with the budget below ONE marker's working set (object
              blocks): identical results, tiles read / total reads / reread
              factor / peak working set / peak RSS.
  memory  500 000 objects x 3 markers x 3 regions x 4 statistics through the
          engine and the h5ad writer, in a fresh process: peak RSS.

    python scripts/verify_step4_s43.py real   [--output-dir DIR]
    python scripts/verify_step4_s43.py memory [--output-dir DIR]
"""

import argparse
import datetime
import json
import os
import resource
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from block01.core import quant_engine as qe  # noqa: E402
from block01.core import quant_sources as qs  # noqa: E402
from block01.workers import feature_extract_worker as few  # noqa: E402

RUN = ("~/fusion_data/test1/rois/full_wsi_20260927_121444_6bad/step2/segmentation_runs/"
       "seg_20260927_124316_stardist_nuclei_expansion")
S41_CSV = ("~/fusionflux/bench_step4/test1_tophat/2026-09-27_70b4565+wt_s41/product_1/"
           "cell_features.csv")
MARKERS = ["DAPI", "CD3D", "FOXP3"]


def _git():
    try:
        return subprocess.run(["git", "-C", HERE, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


def _peak_gb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def reference(lab, nuc, table, img, ids, region):
    """numpy per object, over the pixel-defined region."""
    inside = (lab > 0) & (nuc > 0) & (table[nuc] == lab)
    mask = {"cell": lab > 0, "nucleus": inside, "cytoplasm": (lab > 0) & ~inside}[region]
    labs = lab[mask].astype(np.int64)
    vals = img[mask].astype(np.float64)
    order = np.lexsort((vals, labs))
    labs, vals = labs[order], vals[order]
    edges = np.searchsorted(labs, ids)
    ends = np.searchsorted(labs, ids, side="right")
    out = {s: np.full(ids.size, np.nan) for s in qe.DIST_STATS}
    for k in range(ids.size):
        x = vals[edges[k]:ends[k]]
        m = x.size
        if m == 0:
            continue
        out["median"][k] = np.median(x)
        out["p90"][k] = np.percentile(x, 90)
        out["p95"][k] = np.percentile(x, 95)
        tot = x.sum()
        out["gini"][k] = np.nan if tot == 0 else \
            2 * np.sum(np.arange(1, m + 1) * x) / (m * tot) - (m + 1) / m
    return out


def mode_real(out):
    import anndata
    import zarr
    from skimage.measure import regionprops
    run = os.path.expanduser(RUN)
    rep = {"git": _git(), "run": run, "markers": MARKERS}
    t0 = time.perf_counter()
    res = few.run_extraction(run, os.path.join(out, "full"), regions=["nucleus", "cytoplasm"],
                             features=["morphology", "nuclear_summary"],
                             distribution=list(qe.DIST_STATS), markers=MARKERS)
    rep["seconds"] = round(time.perf_counter() - t0, 2)
    rep["peak_rss_gb"] = round(_peak_gb(), 2)
    prov = json.load(open(res["provenance"]))
    rep["timing"] = prov["timing_seconds"]
    rep["distribution"] = prov["distribution"]
    ad = anndata.read_h5ad(res["h5ad"])
    ids = ad.obs["cell_id"].to_numpy().astype(np.int64)
    job = qs.resolve_quant_job(run)
    lab = zarr.open(job.label_path, mode="r")[:]
    nuc = zarr.open(job.nucleus_path, mode="r")[:]
    table = zarr.open(job.table_path, mode="r")[:]
    cmp = {}
    r = qs.JobReader(job)
    try:
        for mk in [m for m in [c.name for c in job.channels] if m in MARKERS]:
            ch = next(c for c in job.channels if c.name == mk)
            img = r.channels([ch], 0, lab.shape[0], 0, lab.shape[1])[0]
            for region in ("cell", "nucleus", "cytoplasm"):
                ref = reference(lab, nuc, table, img, ids, region)
                for stat in qe.DIST_STATS:
                    got = ad.obsm[f"{region}_{stat}"][mk].to_numpy().astype(np.float64)
                    want = ref[stat]
                    ok = ~np.isnan(want)
                    rel = np.abs(got[ok] - want[ok]) / np.maximum(np.abs(want[ok]), 1e-12)
                    cmp[f"{mk}:{region}_{stat}"] = {
                        "nan_equal": bool(np.array_equal(np.isnan(got), np.isnan(want))),
                        "max_rel": float(rel.max()) if rel.size else 0.0}
            del img
    finally:
        r.close()
    del nuc
    rep["vs_numpy_worst_rel"] = max(v["max_rel"] for v in cmp.values())
    rep["vs_numpy_nan_equal"] = all(v["nan_equal"] for v in cmp.values())
    rep["vs_numpy"] = cmp
    t0 = time.perf_counter()
    props = {p.label: p.perimeter_crofton for p in regionprops(lab)}
    want = np.array([props[i] for i in ids])
    got = ad.obs["perimeter_crofton"].to_numpy()
    rep["crofton_vs_skimage_max_abs"] = float(np.abs(got - want).max())
    rep["crofton_seconds"] = round(time.perf_counter() - t0, 1)
    rep["circularity_max"] = float(np.nanmax(ad.obs["circularity"].to_numpy()))
    del lab, ad
    # the fast statistics: the columns shared with S4-1's product CSV
    s41 = os.path.expanduser(S41_CSV)
    if os.path.exists(s41):
        r2 = few.run_extraction(run, os.path.join(out, "cell_only"), write_csv=True)
        a = [ln.split(",") for ln in open(r2["csv"]).read().splitlines()]
        b = [ln.split(",") for ln in open(s41).read().splitlines()]
        shared = [c for c in b[0] if c in a[0]]
        ia = [a[0].index(c) for c in shared]
        ib = [b[0].index(c) for c in shared]
        same = len(a) == len(b) and all([ra[i] for i in ia] == [rb[i] for i in ib]
                                        for ra, rb in zip(a[1:], b[1:]))
        rep["fast_columns_equal_s41"] = {"columns": len(shared), "identical": bool(same)}
    # the object-block path: a budget below ONE marker's working set
    one = 20 * 2 ** 20          # below one uint8 marker's buffer (~26 MB here): blocks
    t0 = time.perf_counter()
    res3 = few.run_extraction(run, os.path.join(out, "blocks"), regions=["nucleus", "cytoplasm"],
                              distribution=list(qe.DIST_STATS), markers=MARKERS,
                              settings=qe.QuantSettings(accumulator_budget=one))
    rep["blocks_seconds"] = round(time.perf_counter() - t0, 2)
    p3 = json.load(open(res3["provenance"]))
    rep["blocks"] = p3["distribution"]
    a1 = anndata.read_h5ad(res["h5ad"])
    a3 = anndata.read_h5ad(res3["h5ad"])
    rep["blocks_identical"] = all(
        np.array_equal(a1.obsm[k].to_numpy(), a3.obsm[k].to_numpy(), equal_nan=True)
        for k in a1.obsm.keys())
    rep["peak_rss_gb_end"] = round(_peak_gb(), 2)
    return rep


def memory_child(out, n_objects):
    import types
    from verify_step4_s42 import GridReader
    side = int(np.ceil(np.sqrt(n_objects))) * 4
    reader = GridReader(side, 29)
    n = int(reader.lab.max())
    channels = tuple(qs.ChannelSource(name=f"ch{i}", index=i, decision="original", kind="raw",
                                      path="") for i in range(29))
    job = types.SimpleNamespace(shape=reader.shape, bbox=(0, reader.shape[0], 0, reader.shape[1]),
                                n_objects=n, channels=channels, compartment="cell",
                                has_nuclei=True, seam_merge={"version": 1}, nucleus_path="x",
                                table_path="x", n_nuclei=n)
    base = _peak_gb()
    t0 = time.perf_counter()
    res, timing = qe.quantify(job, reader, qe.FAST_STATS, regions=["nucleus", "cytoplasm"],
                              features=["morphology", "nuclear_summary"],
                              sink_path=os.path.join(out, "sink"),
                              distribution=list(qe.DIST_STATS), markers=["ch0", "ch5", "ch9"])
    t_q = time.perf_counter() - t0
    t0 = time.perf_counter()
    few._write_h5ad(os.path.join(out, "memory.h5ad"), job, res, {"synthetic": True})
    t_w = time.perf_counter() - t0
    print(json.dumps({"objects": n, "markers": 3, "regions": 3, "dist_stats": 4,
                      "baseline_peak_gb": round(base, 2), "peak_gb": round(_peak_gb(), 2),
                      "distribution": res.distribution, "seconds_quantify": round(t_q, 1),
                      "seconds_h5ad": round(t_w, 1)}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["real", "memory", "memory-child"])
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--objects", type=int, default=500_000)
    args = ap.parse_args()
    out = os.path.expanduser(args.output_dir) if args.output_dir else os.path.expanduser(
        f"~/fusionflux/bench_step4/test1_tophat/{datetime.date.today()}_{_git()}_s43")
    os.makedirs(out, exist_ok=True)
    if args.mode == "memory-child":
        memory_child(out, args.objects)
        return
    if args.mode == "memory":
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "memory-child",
                            "--output-dir", out, "--objects", str(args.objects)],
                           capture_output=True, text=True)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        if r.returncode or not line:
            raise SystemExit(r.stdout + r.stderr)
        rep = json.loads(line[-1])
        print(json.dumps(rep, indent=1))
        json.dump(rep, open(os.path.join(out, "memory.json"), "w"), indent=1)
        return
    rep = mode_real(out)
    print(json.dumps({k: v for k, v in rep.items() if k != "vs_numpy"}, indent=1, default=str))
    json.dump(rep, open(os.path.join(out, "real.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
