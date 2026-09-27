"""
Block S4-1 real-data acceptance (not imported by the product).

Runs the PRODUCT Step4 path (`run_extraction`) read-only on a project's run
and checks it against independent references:

  * area, centroid and mean / sum / std / min / max of every channel against
    the S4-0 reference B CSV (Step0's corrected product + the old algorithm;
    6 significant digits);
  * axis lengths, eccentricity and orientation against skimage `regionprops`
    on the whole mask;
  * `boundary_pixel_count` against a whole-mask label-aware 3x3 reference;
  * the raw tile reader against tifffile's zarr store on sampled blocks;
  * the provenance: valid cells, empty labels, the source of every channel.

Outputs (outside the repo): the product CSV / provenance and `verify.json`
in --output-dir. Nothing is written into the project.

    python scripts/verify_step4_s41.py --run <run folder> \
        --reference <S4-0 fast_corrected cell_features.csv> [--output-dir DIR]
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

from block01.core import quant_engine as qe  # noqa: E402
from block01.core import quant_sources as qs  # noqa: E402
from block01.workers import feature_extract_worker as few  # noqa: E402

RUN = ("~/fusion_data/test1/rois/full_wsi_20260927_121444_6bad/step2/segmentation_runs/"
       "seg_20260927_124316_stardist_nuclei_expansion")
REF = ("~/fusionflux/bench_step4/test1_tophat/2026-09-27_bdbd29e/fast_corrected/fast_corrected/"
       "cell_features.csv")


def _git():
    try:
        return subprocess.run(["git", "-C", HERE, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


def _load_csv(path):
    with open(path) as f:
        header = f.readline().strip().split(",")
    data = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    return header, data


def _rel(a, b):
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    return np.abs(a - b) / np.maximum(np.abs(b), 1e-12)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=RUN)
    ap.add_argument("--reference", default=REF)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--repeat", type=int, default=2, help="product runs (timing)")
    args = ap.parse_args()
    run = os.path.expanduser(args.run)
    out = os.path.expanduser(args.output_dir) if args.output_dir else os.path.expanduser(
        f"~/fusionflux/bench_step4/test1_tophat/{datetime.date.today()}_{_git()}_s41")
    os.makedirs(out, exist_ok=True)
    report = {"git": _git(), "run": run, "reference": args.reference, "output_dir": out}

    # 1. the product path, timed (the first run includes Numba's cache load)
    runs = []
    for i in range(args.repeat):
        t0 = time.perf_counter()
        res = few.run_extraction(run, os.path.join(out, f"product_{i}"))
        runs.append({"seconds": time.perf_counter() - t0,
                     "peak_rss_gb_so_far": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6,
                     "provenance": res["provenance"]})
        print(f"product run {i}: {runs[-1]['seconds']:.1f} s", flush=True)
    report["product_runs"] = runs
    prov = json.load(open(runs[-1]["provenance"]))
    report["timing_last_run"] = prov["timing_seconds"]
    report["provenance_summary"] = {k: prov.get(k) for k in (
        "morphology_version", "perimeter_definition", "primary_compartment", "max_label_id",
        "n_valid_cells", "n_empty_labels", "empty_label_ids", "reads", "raw_reader")}
    report["channel_sources"] = {c["name"]: c["source"] for c in prov["channels"]}
    header, prod = _load_csv(os.path.join(os.path.dirname(runs[-1]["provenance"]),
                                          "cell_features.csv"))
    col = {c: i for i, c in enumerate(header)}
    ids = prod[:, 0].astype(np.int64)

    # 2. against the S4-0 reference B CSV
    rh, ref = _load_csv(os.path.expanduser(args.reference))
    rcol = {c: i for i, c in enumerate(rh)}
    ref_ids = ref[:, rcol["cell_id"]].astype(np.int64)
    pos = {v: i for i, v in enumerate(ref_ids)}
    ri = np.array([pos[v] for v in ids])
    missing = sorted(set(ref_ids) - set(ids))
    cmp = {"ids_not_in_product": [int(v) for v in missing],
           "ref_area_of_those": [float(ref[pos[v], rcol["area"]]) for v in missing]}
    cmp["area_identical"] = bool(np.array_equal(prod[:, col["area"]], ref[ri, rcol["area"]]))
    worst = {}
    over = 0
    # the old axis lengths / eccentricity carry float32 error: compared to
    # skimage below, and their difference from the old values only reported
    changed = ("major_axis", "minor_axis", "eccentricity")
    for name in [c for c in header if c in rcol and c not in ("cell_id", "area") + changed]:
        r = _rel(prod[:, col[name]], ref[ri, rcol[name]])
        worst[name] = float(r.max())
        over += int((r > 1e-5).sum())
    cmp["old_float32_columns_max_abs_difference"] = {
        name: float(np.abs(prod[:, col[name]] - ref[ri, rcol[name]]).max()) for name in changed}
    cmp["values_over_1e-5"] = over
    cmp["worst_relative"] = max(worst.values())
    cmp["worst_column"] = max(worst, key=worst.get)
    cmp["columns_compared"] = len(worst)
    report["vs_s40_reference"] = cmp
    print("vs S4-0:", {k: v for k, v in cmp.items() if k != "ref_area_of_those"}, flush=True)

    # 3. full precision: the engine once more, then skimage and the boundary reference
    import zarr
    from scipy import ndimage as ndi
    from skimage.measure import regionprops
    job = qs.resolve_quant_job(run)
    reader = qs.JobReader(job)
    try:
        result, _t = qe.quantify(job, reader, ["mean"])
    finally:
        reader.close()
    fcol = {c: i for i, c in enumerate(result.columns)}
    lab = zarr.open(job.label_path, mode="r")[:]
    t0 = time.perf_counter()
    props = regionprops(lab)
    by = {p.label: p for p in props}
    rids = result.cell_ids.astype(np.int64)
    sk = {}
    for name, fn in [("major_axis", lambda p: p.axis_major_length),
                     ("minor_axis", lambda p: p.axis_minor_length),
                     ("eccentricity", lambda p: p.eccentricity),
                     ("orientation", lambda p: p.orientation)]:
        want = np.array([fn(by[i]) for i in rids])
        got = result.values[:, fcol[name]]
        d = np.abs(got - want)
        if name == "orientation":                # +-pi/2 is the same axis
            d = np.minimum(d, np.abs(np.abs(got - want) - np.pi))
        sk[name] = {"max_abs": float(d.max()), "over_1e-6": int((d > 1e-6).sum())}
    sk["cells"] = int(rids.size)
    sk["seconds"] = time.perf_counter() - t0
    report["vs_skimage"] = sk
    print("vs skimage:", sk, flush=True)
    t0 = time.perf_counter()
    mx = ndi.maximum_filter(lab, size=3, mode="constant", cval=0)
    edge = mx != lab
    del mx
    mn = ndi.minimum_filter(lab, size=3, mode="constant", cval=0)
    edge |= mn != lab
    del mn
    edge &= lab > 0
    bref = np.bincount(lab[edge], minlength=job.n_objects + 1)
    del edge
    got = result.values[:, fcol["boundary_pixel_count"]].astype(np.int64)
    report["boundary_vs_reference"] = {"identical": bool(np.array_equal(got, bref[rids])),
                                       "differing_cells": int((got != bref[rids]).sum()),
                                       "seconds": time.perf_counter() - t0}
    print("boundary:", report["boundary_vs_reference"], flush=True)
    del lab

    # 4. the raw tile reader against tifffile's zarr store
    import tifffile
    rng = np.random.default_rng(0)
    r = qs.TiffTileReader(job.slide)
    tif = tifffile.TiffFile(job.slide)
    z = zarr.open(tif.series[0].levels[0].aszarr(), mode="r")
    z = z["0"] if hasattr(z, "keys") else z
    H, W = r.shape
    same = 0
    for _ in range(10):
        h, w = rng.integers(1, 3000, size=2)
        y0, x0 = rng.integers(0, H - h), rng.integers(0, W - w)
        ch = sorted(rng.choice(z.shape[0], size=3, replace=False).tolist())
        a = r.read(ch, y0, y0 + h, x0, x0 + w)
        b = np.stack([z[c, y0:y0 + h, x0:x0 + w] for c in ch])
        same += int(np.array_equal(a, b))
    r.close()
    tif.close()
    report["raw_reader_blocks_identical"] = f"{same}/10"
    print("raw reader:", report["raw_reader_blocks_identical"], flush=True)

    with open(os.path.join(out, "verify.json"), "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"-> {os.path.join(out, 'verify.json')}")


if __name__ == "__main__":
    main()
