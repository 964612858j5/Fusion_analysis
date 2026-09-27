"""
block01/workers/feature_extract_worker.py — Step4's per-cell quantification
(block S4-1).

One region of one Step2 run: its LabelStore labels, every slide channel read
from where Step0 decided (`core/quant_sources.py`, fail-closed), one streaming
pass (`core/quant_engine.py`). Writes

  <base>.csv              one row per cell WITH pixels: cell_id, morphology,
                          <channel>_<statistic>
  <base>_provenance.json  what was quantified from where, and how

both through `*.partial` files renamed only when everything is written; a
failure or a Stop leaves neither.
"""

import datetime
import json
import os
import subprocess
import time
import traceback

import numpy as np

from PyQt5.QtCore import QThread, pyqtSignal

from ..core import quant_engine as qe
from ..core import quant_sources as qs

INTEGER_COLUMNS = {"cell_id", "area", "bbox_min_y", "bbox_min_x", "bbox_max_y",
                   "bbox_max_x", "boundary_pixel_count"}
EMPTY_IDS_LISTED_UP_TO = 1000


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
    fmt = ["%d" if c in INTEGER_COLUMNS else "%.6g" for c in result.columns]
    np.savetxt(path, result.values, delimiter=",", header=",".join(result.columns),
               comments="", fmt=fmt)


def _provenance(job, result, stats, reader, settings, timing, csv_name, seconds):
    empty = [int(v) for v in result.empty_label_ids]
    prov = {
        "schema": "block01.step4.quantification",
        "schema_version": 1,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "morphology_version": qe.MORPHOLOGY_VERSION,
        "perimeter_definition": qe.PERIMETER_DEFINITION,
        "primary_compartment": job.compartment,
        "statistics": list(stats),
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
                     "queue_depth": settings.queue_depth},
        "timing_seconds": {k: (round(v, 3) if isinstance(v, float) else v)
                           for k, v in timing.items()},
        "total_seconds": round(seconds, 3),
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "outputs": {"csv": csv_name},
    }
    if len(empty) <= EMPTY_IDS_LISTED_UP_TO:
        prov["empty_label_ids"] = empty
    return prov


def run_extraction(run_path, output_dir, roi_name=None, statistics=None, file_prefix=None,
                   open_slide=None, settings=None, progress=None, should_stop=None):
    """Quantify one region and write its outputs: {"csv", "provenance"}.
    Raises QuantSourceError (nothing quantified), QuantStopped, or any I/O
    error -- never leaving a `*.partial` or half an output behind."""
    t_start = time.perf_counter()
    settings = settings or qe.QuantSettings()
    stats = qe.normalize_statistics(statistics or qe.FAST_STATS)
    job = qs.resolve_quant_job(run_path, roi_name, open_slide)
    reader = qs.JobReader(job, read_threads=settings.read_threads)
    try:
        result, timing = qe.quantify(job, reader, stats, settings=settings,
                                     progress=progress, should_stop=should_stop)
    finally:
        reader.close()
    if should_stop is not None and should_stop():
        raise qe.QuantStopped()
    os.makedirs(output_dir, exist_ok=True)
    base = output_base(file_prefix)
    csv_path = os.path.join(output_dir, f"{base}.csv")
    prov_path = os.path.join(output_dir, f"{base}_provenance.json")
    partials = [csv_path + ".partial", prov_path + ".partial"]
    try:
        t0 = time.perf_counter()
        _write_csv(partials[0], result)
        timing["write_csv"] = time.perf_counter() - t0
        prov = _provenance(job, result, stats, reader, settings, timing,
                           os.path.basename(csv_path), time.perf_counter() - t_start)
        with open(partials[1], "w", encoding="utf-8") as f:
            json.dump(prov, f, indent=2)
        os.replace(partials[0], csv_path)
        try:
            os.replace(partials[1], prov_path)
        except OSError:
            os.remove(csv_path)          # a table without its provenance is half an output
            raise
    finally:
        for p in partials:
            if os.path.exists(p):
                os.remove(p)
    return {"csv": csv_path, "provenance": prov_path, "job": job,
            "n_cells": int(result.cell_ids.size)}


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
                 file_prefix=None, open_slide=None, settings=None):
        super().__init__()
        self.run_path = run_path
        self.output_dir = output_dir
        self.roi_name = roi_name
        self.statistics = list(statistics or qe.FAST_STATS)
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
                statistics=self.statistics, file_prefix=self.file_prefix,
                open_slide=self.open_slide, settings=self.settings,
                progress=lambda d, t, m: self.progress.emit(d, max(1, t), m),
                should_stop=lambda: self._stop)
            self.progress.emit(1, 1, f"{self.outputs['n_cells']:,} cells → "
                                     f"{self.outputs['csv']}")
            self.extraction_done.emit(self.output_dir, self.base_name)
        except qe.QuantStopped:
            self.error.emit("Stopped by user.")
        except (qs.QuantSourceError, qe.QuantLabelError) as exc:
            self.error.emit(str(exc))
        except Exception:                                       # noqa: BLE001
            self.error.emit(traceback.format_exc())
