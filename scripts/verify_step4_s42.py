"""
Block S4-2 acceptance on real data and the memory gate (not imported by the
product). Read-only on the projects; outputs outside the repo.

  real    the PRODUCT path (`run_extraction`) on a run made after Step2's seam
          fix and on one made before it:
            * whole-cell values: the cell-only CSV equals S4-1's product CSV
              character by character (the run S4-1 verified);
            * nucleus / cytoplasm: against an independent whole-image
              reference (scipy.ndimage over the pixel-defined regions) for a
              raw and a corrected channel;
            * nucleus pixels outside their cell: 0 after the fix, reported
              before it; time, peak memory; the h5ad read back by anndata.
  memory  500 000 objects x 29 channels x 3 regions x 5 statistics through the
          real engine, sink and h5ad writer (synthetic 4 x 4 px cells with a
          nucleus each), in a fresh process: its peak resident memory.

    python scripts/verify_step4_s42.py real   [--output-dir DIR]
    python scripts/verify_step4_s42.py memory [--output-dir DIR] [--objects N]
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

NEW_RUN = ("~/fusion_data/test1/rois/full_wsi_20260927_194511_cc8e/step2/segmentation_runs/"
           "seg_20260927_194616_stardist_nuclei_expansion")
OLD_RUN = ("~/fusion_data/test1/rois/full_wsi_20260927_121444_6bad/step2/segmentation_runs/"
           "seg_20260927_124316_stardist_nuclei_expansion")
S41_CSV = ("~/fusionflux/bench_step4/test1_tophat/2026-09-27_70b4565+wt_s41/product_1/"
           "cell_features.csv")


def _git():
    try:
        return subprocess.run(["git", "-C", HERE, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


def _peak_gb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def region_reference(job, channel):
    """float64 per-object mean / min / max of one channel over the cell,
    nucleus and cytoplasm regions, straight from the whole arrays."""
    import zarr
    from scipy import ndimage as ndi
    lab = zarr.open(job.label_path, mode="r")[:]
    nuc = zarr.open(job.nucleus_path, mode="r")[:]
    table = zarr.open(job.table_path, mode="r")[:]
    inside = (lab > 0) & (nuc > 0) & (table[nuc] == lab)
    del nuc
    r = qs.JobReader(job)
    try:
        img = r.channels([channel], 0, lab.shape[0], 0, lab.shape[1])[0].astype(np.float32)
    finally:
        r.close()
    ids = np.nonzero(np.bincount(lab.ravel()))[0]
    ids = ids[ids > 0]
    out = {}
    for region, m in (("cell", lab), ("nucleus", np.where(inside, lab, 0)),
                      ("cytoplasm", np.where((lab > 0) & ~inside, lab, 0))):
        cnt = np.bincount(m.ravel(), minlength=lab.max() + 1)[ids]
        with np.errstate(invalid="ignore"):
            vals = {"mean": ndi.mean(img, m, ids), "min": ndi.minimum(img, m, ids),
                    "max": ndi.maximum(img, m, ids)}
        out[region] = {k: np.where(cnt > 0, v, np.nan) for k, v in vals.items()}
        del m
    return ids, out


def mode_real(out):
    import anndata
    report = {}
    for tag, run in (("after_seam_fix", NEW_RUN), ("before_seam_fix", OLD_RUN)):
        run = os.path.expanduser(run)
        job = qs.resolve_quant_job(run)
        rec = {"run": run, "seam_merge": job.seam_merge or "absent", "n_objects": job.n_objects,
               "n_nuclei": job.n_nuclei}
        t0 = time.perf_counter()
        res = few.run_extraction(run, os.path.join(out, tag, "full"),
                                 regions=["nucleus", "cytoplasm"],
                                 features=["morphology", "nuclear_summary"])
        rec["seconds_full_scope"] = round(time.perf_counter() - t0, 2)
        rec["peak_rss_gb_so_far"] = round(_peak_gb(), 2)
        prov = json.load(open(res["provenance"]))
        rec["timing"] = prov["timing_seconds"]
        rec["channel_groups"] = prov["channel_groups"]
        rec["nucleus_pixels_outside_their_cell"] = prov["nuclei"][
            "nucleus_pixels_outside_their_cell"]
        ad = anndata.read_h5ad(res["h5ad"])
        rec["h5ad"] = {"shape": list(ad.shape), "layers": sorted(ad.layers.keys()),
                       "obs": list(ad.obs.columns), "X_statistic": ad.uns["X_statistic"],
                       "size_mb": round(os.path.getsize(res["h5ad"]) / 1e6, 1)}
        # independent region reference: DAPI (raw) and the first corrected channel
        picks = [c for c in job.channels if c.name == "DAPI"] + \
            [c for c in job.channels if c.kind == "corrected"][:1]
        cmp = {}
        for ch in picks:
            ids, ref = region_reference(job, ch)
            assert np.array_equal(ids, ad.obs["cell_id"].to_numpy())
            ci = [c.name for c in job.channels].index(ch.name)
            for region in ("cell", "nucleus", "cytoplasm"):
                for stat in ("mean", "min", "max"):
                    got = np.asarray(ad.layers[f"{region}_{stat}"])[:, ci].astype(np.float64)
                    want = ref[region][stat]
                    same_nan = bool(np.array_equal(np.isnan(got), np.isnan(want)))
                    ok = ~np.isnan(want)
                    rel = np.abs(got[ok] - want[ok]) / np.maximum(np.abs(want[ok]), 1e-12)
                    cmp[f"{ch.name}:{region}_{stat}"] = {"nan_pattern_equal": same_nan,
                                                         "max_rel": float(rel.max())
                                                         if rel.size else 0.0}
        rec["vs_region_reference"] = cmp
        del ad
        # whole-cell values: S4-1's product CSV, character by character (the old run)
        if tag == "before_seam_fix" and os.path.exists(os.path.expanduser(S41_CSV)):
            r2 = few.run_extraction(run, os.path.join(out, tag, "cell_only"), write_csv=True)
            a = open(r2["csv"]).read()
            b = open(os.path.expanduser(S41_CSV)).read()
            rec["cell_only_csv_equals_s41"] = a == b
        report[tag] = rec
        print(tag, json.dumps({k: v for k, v in rec.items() if k != "vs_region_reference"},
                              default=str), flush=True)
        worst = max(v["max_rel"] for v in cmp.values())
        print(f"  region reference: worst rel {worst:.2e}, nan patterns equal: "
              f"{all(v['nan_pattern_equal'] for v in cmp.values())}", flush=True)
    return report


class GridReader:
    """A synthetic QuantReader: 4 x 4 px cells on a grid, a 2 x 2 nucleus in
    each, 29 uint8 channels."""

    def __init__(self, side, n_ch, seed=0):
        cells = side // 4
        self.shape = (cells * 4, cells * 4)
        grid = np.arange(1, cells * cells + 1, dtype=np.uint32).reshape(cells, cells)
        self.lab = np.kron(grid, np.ones((4, 4), np.uint32))
        nuc_one = np.zeros((4, 4), np.uint32)
        nuc_one[1:3, 1:3] = 1
        self.nuc = self.lab * np.tile(nuc_one, (cells, cells))
        self.table = np.arange(0, cells * cells + 1, dtype=np.uint32)
        rng = np.random.default_rng(seed)
        self.img = rng.integers(0, 256, size=(n_ch,) + self.shape, dtype=np.uint8)
        self.reads = {"raw": 0, "corrected": 0}
        self.raw_mode = "synthetic"

    def channel_dtype(self, ch):
        return np.dtype(np.uint8)

    def labels(self, y0, y1, x0, x1):
        H, W = self.shape
        out = np.zeros((y1 - y0 + 2, x1 - x0 + 2), np.uint32)
        ry0, ry1, rx0, rx1 = max(0, y0 - 1), min(H, y1 + 1), max(0, x0 - 1), min(W, x1 + 1)
        out[ry0 - y0 + 1:ry1 - y0 + 1, rx0 - x0 + 1:rx1 - x0 + 1] = self.lab[ry0:ry1, rx0:rx1]
        return out

    def nuclei(self, y0, y1, x0, x1):
        return np.ascontiguousarray(self.nuc[y0:y1, x0:x1])

    def nucleus_table(self):
        return self.table

    def channels(self, sources, y0, y1, x0, x1):
        return self.img[[s.index for s in sources], y0:y1, x0:x1]


def memory_child(out, n_objects):
    """In a fresh process: build the synthetic inputs, record the baseline,
    run engine + sink + h5ad, report the peak above and including it."""
    import types
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
                              sink_path=os.path.join(out, "sink"))
    t_q = time.perf_counter() - t0
    prov = {"synthetic": True}
    t0 = time.perf_counter()
    few._write_h5ad(os.path.join(out, "memory.h5ad"), job, res, prov)
    t_w = time.perf_counter() - t0
    print(json.dumps({"objects": n, "channels": 29, "regions": 3, "stats": 5,
                      "input_arrays_gb": round((reader.img.nbytes + reader.lab.nbytes * 2) / 1e9, 2),
                      "baseline_peak_gb": round(base, 2), "peak_gb": round(_peak_gb(), 2),
                      "peak_above_inputs_gb": round(_peak_gb() - base, 2),
                      "channel_groups": timing["channel_groups"],
                      "seconds_quantify": round(t_q, 1), "seconds_h5ad": round(t_w, 1),
                      "h5ad_mb": round(os.path.getsize(os.path.join(out, "memory.h5ad")) / 1e6)}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["real", "memory", "memory-child"])
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--objects", type=int, default=500_000)
    args = ap.parse_args()
    out = os.path.expanduser(args.output_dir) if args.output_dir else os.path.expanduser(
        f"~/fusionflux/bench_step4/test1/{datetime.date.today()}_{_git()}_s42")
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
    json.dump(rep, open(os.path.join(out, "real.json"), "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
