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

from ..qt.QtCore import QThread, Signal

from ..core import quant_engine as qe
from ..core import quant_sources as qs
from ..core.provenance import register_step4
from ..core.regions import quant_apply as qa
from ..utils import run_store

INTEGER_COLUMNS = qe.INTEGER_COLUMNS
EMPTY_IDS_LISTED_UP_TO = 1000
CSV_BLOCK_ROWS = 65536
H5AD_BLOCK_ROWS = 65536
#: threads of the Rust CSV formatter (it gains little beyond ~16) and the
#: rows it formats at once (a part of a sink block: their text is in memory)
CSV_THREADS = max(1, min(16, os.cpu_count() or 1))
CSV_RUST_BLOCK_ROWS = 16384


def _csv_formatter():
    """The viewer core's `format_csv_rows` (plan docs/v17_step4_x_stream_plan.md
    §1 B), or None: an older or missing core keeps NumPy's `savetxt`, the
    same characters, slower."""
    try:
        import fusion_viewer_core
        return getattr(fusion_viewer_core, "format_csv_rows", None)
    except Exception as exc:                   # noqa: BLE001 -- any import failure: savetxt
        print(f"[Step4] CSV written by NumPy (viewer core unavailable: {exc})")
        return None


def output_base(file_prefix=None):
    p = (file_prefix or "").strip()
    return f"{p}_cell_features" if p else "cell_features"


#: the folder name that stands for "a new quant run" (block RM §5): Step4
#: makes ``<workspace>/runs/quant_<stamp>/`` when it starts -- the stamp is
#: the moment Run is pressed, so it cannot be shown before (acceptance
#: 2026-10-05: say so, and show the real folder once it exists).
NEW_QUANT_RUN = "quant_<date_time of Run>"


def default_output_dir(job_or_run_dir, roi_name=None, workspace=None, run_id=None):
    """A published segment run of a block-RM workspace:
    ``<workspace>/runs/quant_<date_time of Run>/<region>/`` -- a new quant
    run, made when Step4 starts. Otherwise
    `<workspace>/step4/quantification_runs/<segmentation_run_id>/<region>/`."""
    run_dir = ""
    if isinstance(job_or_run_dir, qs.QuantJob):
        job = job_or_run_dir
        workspace, run_id, roi_name = job.workspace, job.run_id, job.roi_name
        run_dir = job.run_dir
    if run_dir and run_store.kind_of(run_dir) == "segment" and run_store.is_done(run_dir):
        return os.path.join(workspace, run_store.RUNS_DIR, NEW_QUANT_RUN,
                            qs.region_folder(roi_name))
    return os.path.join(workspace, "step4", "quantification_runs", str(run_id),
                        qs.region_folder(roi_name))


def _git_commit():
    """The process's code version, `-dirty` marked (review fixes §6c)."""
    from ..core import provenance as prov
    return prov.git_commit() or None


def _peak_rss_bytes():
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except (ImportError, AttributeError, ValueError):
        pass
    try:                                    # Windows: no `resource`; the peak working set
        import psutil
        return int(psutil.Process().memory_info().peak_wset)
    except Exception:                       # noqa: BLE001 -- a record field, not the result
        return None


def _csv_text(values):
    """One text column as CSV fields: quoted when it holds a comma, a quote
    or a line break (RFC 4180)."""
    out = []
    for v in values:
        t = str(v)
        if any(ch in t for ch in ',"\n\r'):
            t = '"' + t.replace('"', '""') + '"'
        out.append(t)
    return np.asarray(out, dtype=object)


def _region_csv_columns(frame):
    """(names, per-column formatter) of the Regions obs columns, in order."""
    out = []
    for name in frame.columns:
        col = frame[name]
        kind = col.dtype.kind
        if kind == "b":
            fmt = lambda v: np.where(np.asarray(v, bool), "True", "False").astype(object)   # noqa: E731
        elif kind in "iu":
            fmt = lambda v: np.asarray([f"{int(x)}" for x in v], dtype=object)              # noqa: E731
        elif kind == "f":                      # lossless: the shortest text that reads back the same
            fmt = lambda v: np.asarray([repr(float(x)) for x in v], dtype=object)           # noqa: E731
        else:
            fmt = _csv_text
        out.append((name, fmt))
    return out


def _write_csv(path, result, regions_frame=None):
    """The table, a row block at a time from the sink. With Regions (F4-4)
    every obs column of the h5ad is written too, text as text, right after
    the id."""
    names = result.columns
    # Lossless (docs/v17_review_fixes_plan.md §5): the CSV is float64 by
    # contract and `%.17g` reads back the same float64; `%.6g` cut centroids
    # above 1e6 px by up to ten pixels.
    fmt = ["%d" if c in INTEGER_COLUMNS else "%.17g" for c in names]
    n = int(result.ids.size)
    extra = _region_csv_columns(regions_frame) if regions_frame is not None else []
    rust = _csv_formatter()
    integer = [c in INTEGER_COLUMNS for c in names]
    # `newline=""`: "\n" written as it is on every system -- a text cell's own
    # line break included (Windows' text mode made it "\r\n": another value)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(",".join([names[0]] + [e[0] for e in extra] + names[1:]) + "\n")
        if rust is not None:
            f.flush()                          # the header, before the bytes below
            _write_csv_rows_rust(f.buffer, result, n, len(names), integer, extra,
                                 regions_frame, rust)
            return
        for r0 in range(0, n, CSV_BLOCK_ROWS):
            r1 = min(n, r0 + CSV_BLOCK_ROWS)
            block = result.block(r0, r1)
            if not extra:
                np.savetxt(f, block, delimiter=",", fmt=fmt)
                continue
            import io
            buf = io.StringIO()
            np.savetxt(buf, block, delimiter=",", fmt=fmt)
            lines = buf.getvalue().splitlines()
            texts = [fn(regions_frame[name].to_numpy()[r0:r1]) for name, fn in extra]
            for i, line in enumerate(lines):
                head, _, rest = line.partition(",")
                cells = [head] + [t[i] for t in texts] + ([rest] if rest else [])
                f.write(",".join(cells) + "\n")


def _write_rows(group, name, n, c, dtype, read):
    """`group[name]`: an (n, c) `dtype` array filled a row block at a time
    from `read(r0, r1)` -- the on-disk layout anndata reads (encoding-type
    array, version 0.2.0). A zero-row array has no chunks (F4-4)."""
    ds = group.create_dataset(name, shape=(n, c), dtype=dtype,
                              chunks=(max(1, min(n, H5AD_BLOCK_ROWS)), max(1, c)) if n else None)
    ds.attrs["encoding-type"] = "array"
    ds.attrs["encoding-version"] = "0.2.0"
    for r0 in range(0, n, H5AD_BLOCK_ROWS):
        r1 = min(n, r0 + H5AD_BLOCK_ROWS)
        ds[r0:r1] = read(r0, r1).astype(dtype)


def _write_csv_rows_rust(out, result, n, cols, integer, extra, regions_frame, rust):
    """The rows as savetxt would write them (`_write_csv`'s NumPy branch),
    formatted by the viewer core on `CSV_THREADS` threads. The sink is read
    by `CSV_BLOCK_ROWS` (its chunk rows); each block is formatted and
    written in parts of `CSV_RUST_BLOCK_ROWS` by one helper thread while the
    next block is read -- at most two blocks and one part's text in memory.
    Line ends "\n" on every system, as the text file writes them."""
    from concurrent.futures import ThreadPoolExecutor

    def emit(block, texts):
        out.write(rust(np.ascontiguousarray(block, np.float64).tobytes(), cols, integer, texts,
                       CSV_THREADS, "\n"))
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="step4-csv") as pool:
        pending = None
        for r0 in range(0, n, CSV_BLOCK_ROWS):            # the sink's own chunk rows
            r1 = min(n, r0 + CSV_BLOCK_ROWS)
            block = result.block(r0, r1)
            # a Regions field may hold a line break: kept as "\n", as the
            # text file (`newline=""`) writes it
            texts = [[str(t) for t in fn(regions_frame[name].to_numpy()[r0:r1])]
                     for name, fn in extra]
            for s0 in range(0, r1 - r0, CSV_RUST_BLOCK_ROWS):
                s1 = min(r1 - r0, s0 + CSV_RUST_BLOCK_ROWS)
                if pending is not None:
                    pending.result()           # its error, if any, is this write's
                pending = pool.submit(emit, block[s0:s1], [t[s0:s1] for t in texts])
            del block, texts
        if pending is not None:
            pending.result()


def _write_h5ad(path, job, result, prov, regions=None):
    """ONE AnnData: a skeleton (obs, var, uns) written by anndata, then X and
    every layer appended with h5py a row block at a time from the sink
    (`_write_rows`) -- no step holds X or the layers whole in memory
    (docs/v17_step4_x_stream_plan.md)."""
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
    if regions is not None:                    # F4-4: typed Regions columns right after the id
        rin, frame = regions
        obs = pd.concat([obs[[result.id_column]], frame, obs.drop(columns=[result.id_column])],
                        axis=1)
    var = pd.DataFrame(index=pd.Index([c.name for c in job.channels]))
    var["slide_index"] = [int(c.index) for c in job.channels]
    var["decision"] = [c.decision for c in job.channels]
    var["source"] = [c.kind for c in job.channels]
    var["correction_method"] = [str((c.identity or {}).get("correction_method") or "")
                                for c in job.channels]
    var["correction_param"] = [str((c.identity or {}).get("correction_param_value") or "")
                               for c in job.channels]
    adata = ad.AnnData(X=None, obs=obs, var=var)       # X: appended below, by row blocks
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
    if regions is not None:
        rin, _frame = regions
        adata.uns["regions"] = qa.uns_regions(rin)
        adata.uns["regions_dropped"] = qa.regions_dropped(rin)
        adata.uns["measurement_qc_codes"] = dict(qa.QC_CODES)
        if rin.tma:
            adata.uns["tma_cores"] = qa.tma_cores_frame(result, rin)
        elif rin.has_cores:                     # cores found without a map sheet (backlog 11)
            adata.uns["cores"] = qa.cores_frame(result, rin)
    adata.write_h5ad(path)
    del adata
    n, c = int(result.ids.size), len(job.channels)
    with h5py.File(path, "a") as f:
        _write_rows(f, "X", n, c, np.float32,
                    lambda r0, r1: result.sink.read(result.x_layer, r0, r1))
        group = f.require_group("layers")
        written = [(name, "f4") for name in result.layers]
        if regions is not None:
            written.append((qa.QC_LAYER, "u1"))          # F4-4: uint8 codes, uns measurement_qc_codes
        for name, dtype in written:
            _write_rows(group, name, n, c, dtype,
                        lambda r0, r1, name=name: result.sink.read(name, r0, r1))
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


class SpatialDataExportError(RuntimeError):
    """The export script failed; its reason is for the user."""


SPATIALDATA_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "scripts", "export_spatialdata.py")


def _spatialdata_shapes(rin):
    """The Regions the run applied, for the export's `regions` shapes: every
    core of the snapshot (`display_cores`: each piece its own ring, never the
    bridged analysis outline) and every exclusion (rings split back with
    `split_keyhole`). Polygons are [x, y] slide pixels."""
    if rin is None:
        return []
    from ..core.regions.display import split_keyhole
    out = []
    for c in rin.snapshot.get("display_cores") or []:
        out.append({"kind": "core", "uid": str(c.get("uid", "")), "position_key": c.get("position_key") or "",
                    "name": c.get("name") or "", "run": bool(c.get("run")), "rings": c.get("polygons") or []})
    for e in rin.snapshot.get("exclusions") or []:
        if len(e.get("polygon") or []) >= 3:
            out.append({"kind": "exclusion", "uid": str(e.get("uid", "")), "position_key": "",
                        "name": str(e.get("type", "")), "run": not e.get("seg_invalid", False),
                        "rings": split_keyhole(e["polygon"])})
    return out


def _export_spatialdata(python, h5_partial, out_partial, job, rin, should_stop, job_path):
    """Run `scripts/export_spatialdata.py` under `python` (another
    environment's, docs/v17_spatialdata_export_plan.md) on the finished
    `.partial` h5ad. Stop terminates the child and waits for it. Returns the
    script's JSON record; raises QuantStopped or SpatialDataExportError."""
    nuclei_only = job.compartment == qs.NUCLEUS
    primary = {"name": "nucleus_labels" if nuclei_only else "cell_labels", "path": job.label_path,
               "instance_key": "nucleus_id" if nuclei_only else "cell_id"}
    secondary = ([{"name": "nucleus_labels", "path": job.nucleus_path}] if job.has_nuclei else [])
    pixel_um = rin.pixel_um if rin is not None else qa._pixel_size(job)
    labels = {"cell": job.label_path} if not nuclei_only else {"nucleus": job.label_path}
    if job.has_nuclei:
        labels["nucleus"] = job.nucleus_path
    spec = {"h5ad": h5_partial, "out": out_partial, "primary": primary, "secondary": secondary,
            "bbox": [int(v) for v in job.bbox], "pixel_um": list(pixel_um) if pixel_um else None,
            "shapes": _spatialdata_shapes(rin), "min_side": 256,
            "uns": {"ome_tiff": job.slide, "source_labels": labels,
                    "source_images": _source_images(job)}}
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(spec, f)
    proc = subprocess.Popen([python, "-I", SPATIALDATA_SCRIPT, job_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if should_stop is not None and should_stop():
                    raise qe.QuantStopped()
    except BaseException:
        proc.terminate()                         # Stop or an error here: the child goes first
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    record = {}
    for line in reversed(out.strip().splitlines()):
        try:
            record = json.loads(line)
            break
        except ValueError:
            continue
    if proc.returncode != 0 or not record.get("ok"):
        why = record.get("error") or (err.strip().splitlines() or ["no output"])[-1]
        raise SpatialDataExportError(f"the SpatialData export failed: {why}")
    return record


def _source_images(job):
    """`uns["source_images"]` of the export (plan §1): the slide (path and
    file identity) and, per channel, what Step4 measured -- the slide or
    Step0's corrected product (path, array, its recorded identity)."""
    try:
        st = os.stat(job.slide)
        slide = {"path": job.slide, "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
    except OSError:
        slide = {"path": job.slide}
    # keyed by position ("0", "1", ...), the name a value: a channel name
    # may hold "/", which zarr / AnnData read as a path separator
    return {"slide": slide,
            "channels": {str(k): {"name": c.name, "slide_index": int(c.index), "source": c.kind,
                                  "path": c.path, "array": c.array or "",
                                  "identity_json": json.dumps(c.identity or {}, sort_keys=True,
                                                              default=str)}
                         for k, c in enumerate(job.channels)}}


def _remove(path):
    """A file or a directory (the SpatialData zarr)."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.remove(path)


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
                   distribution=None, markers=None, spatialdata=False, spatialdata_python=None):
    """Quantify one region and write its outputs: {"h5ad", "csv" (or None),
    "spatialdata" (or None), "provenance"}. `spatialdata`: also a SpatialData
    zarr, written by `spatialdata_python` (else `utils.spatialdata_python`). Raises QuantSourceError (nothing quantified),
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
    try:                                       # F4-4: the run's Regions facts, checked now
        rin = qa.load_regions(job)
    except qa.RegionsInputError as exc:
        raise qs.QuantSourceError(str(exc)) from exc
    if qe.normalize_distribution(distribution) and not markers:
        raise ValueError("distribution statistics need at least one marker")
    unknown = [m for m in (markers or []) if m not in [c.name for c in job.channels]]
    if unknown:
        raise ValueError(f"not a channel of this slide: {unknown}")
    if "nuclear_summary" in features and not job.has_nuclei:
        raise ValueError("this run has no nuclei beside its cells: no nuclear summary")
    sd_python, sd_version = None, None
    if spatialdata:                            # refused before any output, like the rest
        from ..utils import spatialdata_python as sdp
        if spatialdata_python:
            ok, info = sdp.check(spatialdata_python)
            sd_python, sd_version = (spatialdata_python, info) if ok else (None, info)
        else:
            sd_python, sd_version = sdp.find()
        if sd_python is None:
            raise ValueError(f"no Python with spatialdata for the SpatialData export ({sd_version})")
    base = output_base(file_prefix)
    quant_run = ""
    if run_store.kind_of(job.run_dir) == "segment" and run_store.is_done(job.run_dir):
        # Block RM (§5): a segment run of a project is quantified into a new
        # quant run (its upstream that segment run), whatever folder was
        # typed -- results of a project stay in its chain.
        quant_run = run_store.new_run(run_store.roi_dir_of(job.run_dir), "quant")
        output_dir = os.path.join(quant_run, qs.region_folder(job.roi_name))
    else:
        cur = os.path.abspath(output_dir)
        while cur != os.path.dirname(cur):
            if run_store.kind_of(cur) and run_store.is_done(cur):
                raise ValueError(f"{output_dir} lies inside a published result "
                                 f"({os.path.basename(cur)}); choose another folder")
            cur = os.path.dirname(cur)
    made_dir = not os.path.isdir(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    sink_dir = os.path.join(output_dir, f".{base}_features.partial")
    h5_path = os.path.join(output_dir, f"{base}.h5ad")
    csv_path = os.path.join(output_dir, f"{base}.csv") if write_csv else None
    sd_path = os.path.join(output_dir, f"{base}.spatialdata.zarr") if spatialdata else None
    sd_job = os.path.join(output_dir, f".{base}_spatialdata_job.json")
    prov_path = os.path.join(output_dir, f"{base}_provenance.json")
    finals = [p for p in (h5_path, csv_path, sd_path, prov_path) if p]
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
        regions_out, qc_stats = None, None
        if rin is not None:                    # F4-4: measurement exclusions -> NaN, typed obs
            qc_stats = qa.apply_measurement_qc(result, rin, should_stop=should_stop)
            regions_out = (rin, qa.obs_frame(result, rin, job.run_id))
        outputs = {"h5ad": os.path.basename(h5_path),
                   "csv": os.path.basename(csv_path) if csv_path else None,
                   "spatialdata": os.path.basename(sd_path) if sd_path else None}
        t0 = time.perf_counter()
        if should_stop is not None and should_stop():
            raise qe.QuantStopped()
        if csv_path:
            _write_csv(csv_path + ".partial", result,
                       regions_out[1] if regions_out is not None else None)
        timing["write_csv"] = time.perf_counter() - t0
        prov = _provenance(job, result, stats, reader, settings, timing, outputs,
                           time.perf_counter() - t_start, features)
        if rin is not None:
            prov["regions"] = qa.provenance_summary(rin, qc_stats)
        t0 = time.perf_counter()
        _write_h5ad(h5_path + ".partial", job, result, prov, regions_out)
        prov["timing_seconds"]["write_h5ad"] = round(time.perf_counter() - t0, 3)
        if sd_path:                            # from the finished h5ad, before anything is published
            if should_stop is not None and should_stop():
                raise qe.QuantStopped()
            t0 = time.perf_counter()
            record = _export_spatialdata(sd_python, h5_path + ".partial", sd_path + ".partial",
                                         job, rin, should_stop, sd_job)
            prov["timing_seconds"]["write_spatialdata"] = round(time.perf_counter() - t0, 3)
            prov["spatialdata"] = {"python": sd_python, "spatialdata_version": sd_version,
                                   **{k: v for k, v in record.items() if k != "ok"}}
        prov["total_seconds"] = round(time.perf_counter() - t_start, 3)
        prov["process_peak_rss_bytes"] = _peak_rss_bytes()
        with open(prov_path + ".partial", "w", encoding="utf-8") as f:
            json.dump(prov, f, indent=2)
        if should_stop is not None and should_stop():
            raise qe.QuantStopped()                # the last moment: nothing is published yet
        for final, part in zip(finals, partials):
            os.replace(part, final)
            renamed.append(final)
        if quant_run:
            run_store.write_params(quant_run, run_store.to_records({
                "kind": "quant",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "summary": f"{job.roi_name} · {len(stats)} statistics"
                           + (f" · {','.join(sorted(features))}" if features else ""),
                "roi_name": job.roi_name, "statistics": list(stats),
                "features": sorted(features), "regions": list(regions or []),
                "distribution": list(distribution or []), "markers": list(markers or []),
                "write_csv": bool(write_csv), "file_prefix": file_prefix or "",
                "outputs": {k: v for k, v in outputs.items() if v},
                "region_folder": os.path.basename(output_dir),
            }, run_store.project_dir_of(quant_run)))
            run_store.write_inputs(quant_run, job.run_dir)
            run_store.publish(quant_run)
        ok = True
        register_step4(h5_path, job, prov)         # block A3: after the commit, never raises
    finally:
        for p in partials + [sd_job]:
            _remove(p)
        if not ok:
            for p in renamed:                 # part of the outputs is half an output
                _remove(p)
        shutil.rmtree(sink_dir, ignore_errors=True)
        if not ok and made_dir:
            try:
                os.rmdir(output_dir)
            except OSError:
                pass
        if not ok and quant_run:
            run_store.discard(quant_run)
    return {"h5ad": h5_path, "csv": csv_path, "spatialdata": sd_path, "provenance": prov_path, "job": job,
            "output_dir": output_dir,
            "n_cells": int(result.ids.size), "nucleus_outside": result.nucleus_outside}


#: The thread's stack (docs/v17_step4_worker_stack_plan.md): numba's LLVM
#: compiles the quantification kernels on this thread when its cache is
#: cold, and overflowed macOS's 512 KB default thread stack (Linux: 8 MB).
STACK_BYTES = 32 * 1024 * 1024


class FeatureExtractWorker(QThread):
    """Step4 in the background.

    Signals:
        progress(done, total, msg)
        extraction_done(output_dir, base_name)
        error(message)   -- a reason for the user, or a traceback
    """

    progress = Signal(int, int, str)
    # Named `extraction_done` so it does NOT shadow QThread's finished().
    extraction_done = Signal(str, str)
    error = Signal(str)

    def __init__(self, run_path, output_dir, roi_name=None, statistics=None,
                 file_prefix=None, open_slide=None, settings=None, regions=None,
                 features=("morphology",), write_csv=False, distribution=None, markers=None,
                 spatialdata=False, spatialdata_python=None):
        super().__init__()
        self.setStackSize(STACK_BYTES)
        self.spatialdata = bool(spatialdata)
        self.spatialdata_python = spatialdata_python
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
                spatialdata=self.spatialdata, spatialdata_python=self.spatialdata_python,
                open_slide=self.open_slide, settings=self.settings,
                progress=lambda d, t, m: self.progress.emit(d, max(1, t), m),
                should_stop=lambda: self._stop)
            self.progress.emit(1, 1, f"{self.outputs['n_cells']:,} objects → "
                                     f"{self.outputs['h5ad']}")
            self.extraction_done.emit(self.outputs.get("output_dir") or self.output_dir,
                                      self.base_name)
        except qe.QuantStopped:
            self.error.emit("Stopped by user.")
        except (qs.QuantSourceError, qe.QuantLabelError, ValueError, SpatialDataExportError) as exc:
            self.error.emit(str(exc))
        except Exception:                                       # noqa: BLE001
            self.error.emit(traceback.format_exc())
