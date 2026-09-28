"""
block01/workers/feature_extract_worker.py — Step4's per-object quantification
(blocks S4-1, S4-2).

One region of one Step2 run: its LabelStore labels, every slide channel read
from where Step0 decided (`core/quant_sources.py`, fail-closed), a streaming
pass per channel group (`core/quant_engine.py`). Writes

  <base>.h5ad             ONE AnnData, one row per primary object (a cell; a
                          nucleus in a nuclei-only run): X = the primary
                          region's first chosen statistic, layers
                          "<region>_<stat>" (X's included), obs = id,
                          morphology, nuclear summary, var = channel sources
  <base>.csv              only when asked: the same table, streamed
  <base>_provenance.json  what was quantified from where, and how

all through `*.partial` files renamed only when everything is written; a
failure or a Stop leaves none of them (nor the on-disk feature sink).
"""

import dataclasses
import datetime
import json
import os
import shutil
import subprocess
import time
import traceback

import numpy as np

from PyQt5.QtCore import QThread, pyqtSignal

from ..core import quant_engine as qe
from ..core import quant_sources as qs

INTEGER_COLUMNS = qe.INTEGER_COLUMNS
EMPTY_IDS_LISTED_UP_TO = 1000
CSV_BLOCK_ROWS = 65536
H5AD_BLOCK_ROWS = 65536


def output_base(file_prefix=None):
    p = (file_prefix or "").strip()
    return f"{p}_cell_features" if p else "cell_features"


def default_output_dir(job_or_run_dir, roi_name=None, workspace=None, run_id=None):
    """`<workspace>/step4/quantification_runs/<segmentation_run_id>/<region>/`."""
    if isinstance(job_or_run_dir, qs.QuantJob):
        job = job_or_run_dir
        workspace, run_id, roi_name = job.workspace, job.run_id, job.roi_name
    return os.path.join(workspace, "step4", "quantification_runs", str(run_id),
                        qs.region_folder(roi_name))


def _git_commit():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _peak_rss_bytes():
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except (ImportError, AttributeError, ValueError):
        return None


def _write_csv(path, result):
    """The table, a row block at a time from the sink."""
    names = result.columns
    fmt = ["%d" if c in INTEGER_COLUMNS else "%.6g" for c in names]
    n = int(result.ids.size)
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(names) + "\n")
        for r0 in range(0, n, CSV_BLOCK_ROWS):
            np.savetxt(f, result.block(r0, min(n, r0 + CSV_BLOCK_ROWS)), delimiter=",",
                       fmt=fmt)


def _write_h5ad(path, job, result, prov):
    """ONE AnnData: a skeleton (X, obs, var, uns) written by anndata, then
    every layer appended with h5py a row block at a time from the sink (the
    on-disk layout anndata reads: encoding-type array, version 0.2.0) -- no
    step holds all layers in memory."""
    import anndata as ad
    import h5py
    import pandas as pd
    obs = pd.DataFrame({result.id_column: result.ids.astype(np.int64)},
                       index=pd.Index(result.ids.astype(str)))
    for name, values in result.obs.items():
        if name in INTEGER_COLUMNS and np.isfinite(values).all():
            obs[name] = values.astype(np.int64)
        else:
            obs[name] = np.asarray(values, np.float64)
    var = pd.DataFrame(index=pd.Index([c.name for c in job.channels]))
    var["slide_index"] = [int(c.index) for c in job.channels]
    var["decision"] = [c.decision for c in job.channels]
    var["source"] = [c.kind for c in job.channels]
    var["correction_method"] = [str((c.identity or {}).get("correction_method") or "")
                                for c in job.channels]
    var["correction_param"] = [str((c.identity or {}).get("correction_param_value") or "")
                               for c in job.channels]
    adata = ad.AnnData(X=result.sink.read(result.x_layer).astype(np.float32), obs=obs, var=var)
    adata.uns["primary_object"] = result.regions[0]
    adata.uns["primary_compartment"] = result.regions[0]
    adata.uns["X_statistic"] = result.stats[0]
    adata.uns["statistics"] = list(result.stats)
    adata.uns["expression_regions"] = list(result.regions)
    adata.uns["morphology_version"] = qe.MORPHOLOGY_VERSION
    adata.uns["perimeter_definition"] = qe.PERIMETER_DEFINITION
    adata.uns["seam_merge"] = dict(job.seam_merge) if job.seam_merge else "absent"
    adata.uns["provenance_json"] = json.dumps(prov)
    adata.uns["perimeter"] = dict(qe.PERIMETER_RECORD)
    if result.dist_markers:
        adata.uns["distribution_markers"] = list(result.dist_markers)
        adata.uns["distribution_statistics"] = list(result.dist_stats)
    adata.write_h5ad(path)
    del adata
    n, c = int(result.ids.size), len(job.channels)
    with h5py.File(path, "a") as f:
        group = f.require_group("layers")
        for name in result.layers:
            ds = group.create_dataset(name, shape=(n, c), dtype="f4",
                                      chunks=(max(1, min(n, H5AD_BLOCK_ROWS)), max(1, c)))
            ds.attrs["encoding-type"] = "array"
            ds.attrs["encoding-version"] = "0.2.0"
            for r0 in range(0, n, H5AD_BLOCK_ROWS):
                r1 = min(n, r0 + H5AD_BLOCK_ROWS)
                ds[r0:r1] = result.sink.read(name, r0, r1).astype(np.float32)
        # block S4-3: distribution statistics of the chosen markers, one
        # DataFrame per region x statistic (columns = the markers), written
        # one at a time
        if result.dist_markers:
            from anndata.io import write_elem
            index = pd.Index(result.ids.astype(str))
            for region in result.regions:
                for stat in result.dist_stats:
                    values = result.sink.read(f"dist_{region}_{stat}").astype(np.float32)
                    frame = pd.DataFrame(values, index=index, columns=list(result.dist_markers))
                    write_elem(f.require_group("obsm"), f"{region}_{stat}", frame)
                    del frame, values


def _provenance(job, result, stats, reader, settings, timing, outputs, seconds, features):
    empty = [int(v) for v in result.empty_label_ids]
    prov = {
        "schema": "block01.step4.quantification",
        "schema_version": 1,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "morphology_version": qe.MORPHOLOGY_VERSION,
        "perimeter_definition": qe.PERIMETER_DEFINITION,
        "primary_compartment": job.compartment,
        "primary_object": result.regions[0],
        "statistics": list(stats),
        "expression_regions": list(result.regions),
        "features": sorted(features),
        "X": result.x_layer,
        "layers": list(result.layers),
        "nuclei": ({"path": job.nucleus_path, "table": job.table_path, "n_nuclei": job.n_nuclei,
                    "nucleus_pixels_outside_their_cell": result.nucleus_outside}
                   if job.has_nuclei else None),
        "seam_merge": job.seam_merge or "absent",
        "perimeter": dict(qe.PERIMETER_RECORD),
        "distribution": result.distribution,
        "segmentation_run": {"run_id": job.run_id, "run_dir": job.run_dir,
                             "method": job.method, "workspace": job.workspace},
        "region": {"roi_name": job.roi_name, "bbox_fullres": list(job.bbox),
                   "shape": list(job.shape)},
        "label_store": {"path": job.label_path, "compartment": job.compartment,
                        "n_objects": job.n_objects},
        "max_label_id": result.max_label_id,
        "n_valid_cells": int(result.cell_ids.size),
        "n_empty_labels": len(empty),
        "slide": job.slide,
        "step0": job.step0,
        "channels": [{"name": c.name, "slide_index": c.index, "decision": c.decision,
                      "source": c.kind, "path": c.path, "array": c.array or None,
                      "identity": c.identity or None} for c in job.channels],
        "reads": dict(reader.reads),
        "raw_reader": reader.raw_mode,
        "backend": "numba",
        "settings": {"tile": settings.tile, "batch_bytes": settings.batch_bytes,
                     "read_threads": settings.read_threads,
                     "compute_threads": settings.compute_threads,
                     "queue_depth": settings.queue_depth,
                     "accumulator_budget": settings.accumulator_budget,
                     "sink_dtype": settings.sink_dtype},
        "channel_groups": result.channel_groups,
        "timing_seconds": {k: (round(v, 3) if isinstance(v, float) else v)
                           for k, v in timing.items()},
        "total_seconds": round(seconds, 3),
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "outputs": outputs,
    }
    if len(empty) <= EMPTY_IDS_LISTED_UP_TO:
        prov["empty_label_ids"] = empty
    return prov


def run_extraction(run_path, output_dir, roi_name=None, statistics=None, regions=None,
                   features=("morphology",), write_csv=False, file_prefix=None,
                   open_slide=None, settings=None, progress=None, should_stop=None,
                   distribution=None, markers=None):
    """Quantify one region and write its outputs: {"h5ad", "csv" (or None),
    "provenance"}. Raises QuantSourceError (nothing quantified),
    QuantStopped, or any I/O error -- never leaving a `*.partial`, the sink or
    half an output behind."""
    t_start = time.perf_counter()
    settings = settings or qe.QuantSettings()
    if write_csv and settings.sink_dtype != "f8":
        # the CSV keeps full precision (S4-1's numbers, character for
        # character); the h5ad's layers are still written as float32
        settings = dataclasses.replace(settings, sink_dtype="f8")
    stats = qe.normalize_statistics(statistics or qe.FAST_STATS)
    features = set(features or ())
    job = qs.resolve_quant_job(run_path, roi_name, open_slide)
    qe.normalize_regions(regions or [], job)                     # refuse before any output
    if qe.normalize_distribution(distribution) and not markers:
        raise ValueError("distribution statistics need at least one marker")
    unknown = [m for m in (markers or []) if m not in [c.name for c in job.channels]]
    if unknown:
        raise ValueError(f"not a channel of this slide: {unknown}")
    if "nuclear_summary" in features and not job.has_nuclei:
        raise ValueError("this run has no nuclei beside its cells: no nuclear summary")
    base = output_base(file_prefix)
    made_dir = not os.path.isdir(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    sink_dir = os.path.join(output_dir, f".{base}_features.partial")
    h5_path = os.path.join(output_dir, f"{base}.h5ad")
    csv_path = os.path.join(output_dir, f"{base}.csv") if write_csv else None
    prov_path = os.path.join(output_dir, f"{base}_provenance.json")
    finals = [p for p in (h5_path, csv_path, prov_path) if p]
    partials = [p + ".partial" for p in finals]
    renamed = []
    ok = False
    try:
        shutil.rmtree(sink_dir, ignore_errors=True)
        reader = qs.JobReader(job, read_threads=settings.read_threads)
        try:
            result, timing = qe.quantify(job, reader, stats, regions=regions, features=features,
                                         sink_path=sink_dir, settings=settings,
                                         progress=progress, should_stop=should_stop,
                                         distribution=distribution, markers=markers)
        finally:
            reader.close()
        if should_stop is not None and should_stop():
            raise qe.QuantStopped()
        outputs = {"h5ad": os.path.basename(h5_path),
                   "csv": os.path.basename(csv_path) if csv_path else None}
        t0 = time.perf_counter()
        if csv_path:
            _write_csv(csv_path + ".partial", result)
        timing["write_csv"] = time.perf_counter() - t0
        prov = _provenance(job, result, stats, reader, settings, timing, outputs,
                           time.perf_counter() - t_start, features)
        t0 = time.perf_counter()
        _write_h5ad(h5_path + ".partial", job, result, prov)
        prov["timing_seconds"]["write_h5ad"] = round(time.perf_counter() - t0, 3)
        prov["total_seconds"] = round(time.perf_counter() - t_start, 3)
        prov["process_peak_rss_bytes"] = _peak_rss_bytes()
        with open(prov_path + ".partial", "w", encoding="utf-8") as f:
            json.dump(prov, f, indent=2)
        for final, part in zip(finals, partials):
            os.replace(part, final)
            renamed.append(final)
        ok = True
    finally:
        for p in partials:
            if os.path.exists(p):
                os.remove(p)
        if not ok:
            for p in renamed:                 # part of the outputs is half an output
                if os.path.exists(p):
                    os.remove(p)
        shutil.rmtree(sink_dir, ignore_errors=True)
        if not ok and made_dir:
            try:
                os.rmdir(output_dir)
            except OSError:
                pass
    return {"h5ad": h5_path, "csv": csv_path, "provenance": prov_path, "job": job,
            "n_cells": int(result.ids.size), "nucleus_outside": result.nucleus_outside}


class FeatureExtractWorker(QThread):
    """Step4 in the background.

    Signals:
        progress(done, total, msg)
        extraction_done(output_dir, base_name)
        error(message)   -- a reason for the user, or a traceback
    """

    progress = pyqtSignal(int, int, str)
    # Named `extraction_done` so it does NOT shadow QThread's finished().
    extraction_done = pyqtSignal(str, str)
    error = pyqtSignal(str)

    def __init__(self, run_path, output_dir, roi_name=None, statistics=None,
                 file_prefix=None, open_slide=None, settings=None, regions=None,
                 features=("morphology",), write_csv=False, distribution=None, markers=None):
        super().__init__()
        self.run_path = run_path
        self.output_dir = output_dir
        self.roi_name = roi_name
        self.statistics = list(statistics or qe.FAST_STATS)
        self.regions = list(regions or [])
        self.features = tuple(features or ())
        self.write_csv = bool(write_csv)
        self.distribution = list(distribution or [])
        self.markers = list(markers or [])
        self.file_prefix = file_prefix
        self.open_slide = open_slide
        self.settings = settings
        self.base_name = output_base(file_prefix)
        self.outputs = None
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            qe.normalize_statistics(self.statistics)
        except ValueError as exc:
            self.error.emit(f"{exc} — Step 4 computes mean, sum, std, min and max")
            return
        try:
            self.progress.emit(0, 1, "Checking the run and Step0's channel sources…")
            self.outputs = run_extraction(
                self.run_path, self.output_dir, roi_name=self.roi_name,
                statistics=self.statistics, regions=self.regions, features=self.features,
                write_csv=self.write_csv, file_prefix=self.file_prefix,
                distribution=self.distribution, markers=self.markers,
                open_slide=self.open_slide, settings=self.settings,
                progress=lambda d, t, m: self.progress.emit(d, max(1, t), m),
                should_stop=lambda: self._stop)
            self.progress.emit(1, 1, f"{self.outputs['n_cells']:,} objects → "
                                     f"{self.outputs['h5ad']}")
            self.extraction_done.emit(self.output_dir, self.base_name)
        except qe.QuantStopped:
            self.error.emit("Stopped by user.")
        except (qs.QuantSourceError, qe.QuantLabelError, ValueError) as exc:
            self.error.emit(str(exc))
        except Exception:                                       # noqa: BLE001
            self.error.emit(traceback.format_exc())
