"""
block01/core/quant_sources.py — where Step4 reads its numbers from (block S4-1).

Qt-free. Two jobs:

1. `resolve_quant_job(run, roi_name, open_slide)` turns a Step2 run and one of
   its regions into a `QuantJob`: the region's labels (the run's LabelStore --
   nothing else), the slide, and for EVERY slide channel the one place its
   intensities come from.

   The scientific contract is fail-closed. Step0's decision `original` (or a
   channel Step0 did not list, e.g. the nucleus channel) reads the raw slide;
   `tophat` / `cucim` reads Step0's persisted corrected product, checked for
   ROI, shape, dtype, method, effective parameter, channel index and source
   slide. A missing or mismatching product stops the job BEFORE anything is
   quantified (`QuantSourceError`). There is no fallback to raw pixels and no
   correction at run time -- this module imports neither `OMETIFFLoader` nor
   any correction operator.

2. The readers behind the `QuantReader` boundary: the quantification kernel
   gets arrays, never TIFF / zarr details. `TiffTileReader` decodes the
   slide's own TIFF tiles in parallel (tifffile's `page.decode`, measured
   ~4 s for 29 channels here vs ~7.5 s through tifffile's zarr store); a
   slide whose layout it does not handle is read through tifffile's zarr
   store instead. That is a READER fallback -- the same pixels, slower --
   never a source fallback.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import step3_masks
from .bg_correction import resolve_effective_correction_params
from .project_identity import ProjectSchemaError, project_dir_of_workspace, read_project_schema

CORRECTED_METHODS = ("tophat", "cucim")
CELL, NUCLEUS = "cell", "nucleus"


class QuantSourceError(Exception):
    """The job cannot be quantified as asked; the message says why."""


# ── the job ──────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class ChannelSource:
    name: str                    # the slide's OME channel name
    index: int                   # its position in the slide
    decision: str                # Step0's decision ("original" when unlisted)
    kind: str                    # "raw" | "corrected"
    path: str                    # the slide, or the corrected zarr
    array: str = ""              # corrected: "<group>/<channel>"
    offset: Tuple[int, int] = (0, 0)   # corrected: region origin inside the array
    identity: Dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class QuantJob:
    run_dir: str
    run_id: str
    method: str
    workspace: str
    roi_name: str
    region: str                  # roi_name made safe for a folder name
    bbox: Tuple[int, int, int, int]    # y0, y1, x0, x1 in slide pixels
    compartment: str             # CELL, or NUCLEUS for a nuclei-only run
    label_path: str
    n_objects: int
    slide: str
    channels: Tuple[ChannelSource, ...]
    step0: Dict[str, Any]
    # block S4-2: the run's nuclei beside its cells (None for a whole-cell or
    # a nuclei-only run), and whether Step2 reconciled its seams (block N3)
    nucleus_path: Optional[str] = None
    table_path: Optional[str] = None
    n_nuclei: int = 0
    seam_merge: Optional[Dict[str, Any]] = None

    @property
    def has_nuclei(self):
        return self.compartment == CELL and self.nucleus_path is not None

    @property
    def shape(self):
        y0, y1, x0, x1 = self.bbox
        return (y1 - y0, x1 - x0)


def region_folder(roi_name):
    """The ROI name as a folder name: spaces become `_` (the corrected
    zarr's group rule), path separators too."""
    return str(roi_name).replace(" ", "_").replace("/", "_").replace("\\", "_")


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def open_run(path):
    """The run at `path` (its folder or a file in it) or QuantSourceError."""
    if not path:
        raise QuantSourceError("no segmentation run chosen")
    run = step3_masks.load_run(path)
    if isinstance(run, str):
        raise QuantSourceError(run)
    return run


def run_regions(run):
    return step3_masks.run_regions(run)


def _label_store(run, roi_name):
    meta = run.meta
    rois = meta.get("rois")
    if isinstance(rois, list):
        mine = [r for r in rois if isinstance(r, dict) and r.get("roi_name") == roi_name]
        if len(mine) != 1:
            raise QuantSourceError(f"this run has no single result for region '{roi_name}'")
        store = mine[0].get("label_store")
        if store is None:
            store = (meta.get("label_store") or {}).get(roi_name) \
                if isinstance(meta.get("label_store"), dict) else None
    elif meta.get("mode") == "full_wsi":
        store = meta.get("label_store")
    else:
        store = None
    if not isinstance(store, dict):
        raise QuantSourceError("this run was made before Step2 kept a label store "
                               "— re-run Step2")
    if store.get("complete") is not True:
        raise QuantSourceError("the run's label store is incomplete — re-run Step2")
    return store


def _nuclei_of(store, compartment, shape, run_dir):
    """(nucleus array path, nucleus -> cell table path, M) of a cell run that
    kept its nuclei; (None, None, 0) otherwise."""
    if compartment != CELL or not store.get(NUCLEUS) or not store.get("nucleus_to_cell"):
        return None, None, 0
    import zarr
    n_entry, t_entry = store[NUCLEUS], store["nucleus_to_cell"]

    def path(p):
        return p if os.path.isabs(p) else os.path.join(run_dir, p)
    npath, tpath = path(n_entry.get("path") or ""), path(t_entry.get("path") or "")
    try:
        nz, tz = zarr.open(npath, mode="r"), zarr.open(tpath, mode="r")
    except Exception as exc:                                    # noqa: BLE001
        raise QuantSourceError(f"the run's nuclei cannot be opened: {exc}")
    m = int(n_entry.get("n_objects", -1))
    if str(nz.dtype) != "uint32" or tuple(nz.shape) != tuple(shape):
        raise QuantSourceError(f"the nucleus array is {nz.dtype} {tuple(nz.shape)}, "
                               f"the region is uint32 {tuple(shape)}")
    if m < 0 or str(tz.dtype) != "uint32" or tuple(tz.shape) != (m + 1,):
        raise QuantSourceError(f"the nucleus -> cell table is {tz.dtype} {tuple(tz.shape)}, "
                               f"not uint32 ({m + 1},)")
    return os.path.realpath(npath), os.path.realpath(tpath), m


def _slide_channels(slide):
    import tifffile
    with tifffile.TiffFile(slide) as tif:
        xml = tif.ome_metadata
        shape = (tif.pages[0].imagelength, tif.pages[0].imagewidth)
    if not xml:
        raise QuantSourceError(f"{os.path.basename(slide)} has no OME channel names")
    ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
    names = [c.get("Name", f"ch_{i:02d}")
             for i, c in enumerate(ET.fromstring(xml).findall(".//ome:Channel", ns))]
    return names, shape


def _corrected_group(root, roi_name, bbox, zpath):
    """The corrected product's array container for this region, and the
    region's origin inside it. roi_only: ONLY the group of this ROI (never
    another ROI's group that happens to hold the channel)."""
    mode = str(root.attrs.get("mode", "")).strip().lower()
    if mode != "roi_only":
        return root, (bbox[0], bbox[2]), None
    hits = []
    for gname in root.group_keys():
        g = root[gname]
        if g.attrs.get("roi_name") == roi_name or gname == region_folder(roi_name):
            hits.append((gname, g))
    if len(hits) != 1:
        raise QuantSourceError(f"the corrected product {zpath} has "
                               f"{'no' if not hits else len(hits)} group(s) for "
                               f"region '{roi_name}'")
    gname, g = hits[0]
    gb = g.attrs.get("bbox_fullres")
    try:
        gy0, gy1, gx0, gx1 = (int(v) for v in gb)
    except (TypeError, ValueError):
        raise QuantSourceError(f"the corrected group '{gname}' records no bbox")
    y0, y1, x0, x1 = bbox
    if not (gy0 <= y0 and y1 <= gy1 and gx0 <= x0 and x1 <= gx1):
        raise QuantSourceError(f"the corrected group '{gname}' covers {[gy0, gy1, gx0, gx1]}, "
                               f"not region {list(bbox)}")
    return g, (y0 - gy0, x0 - gx0), (gy1 - gy0, gx1 - gx0)


def _corrected_decisions(decisions):
    return {str(k): str(v).strip().lower() for k, v in (decisions or {}).items()
            if str(v).strip().lower() in CORRECTED_METHODS}


def _run_data_version(ws, meta):
    """The data-version record a run was made from (its
    ``segmentation_meta.json`` names it), with its folder path; None for a
    run made before data versions."""
    vid = str((meta or {}).get("data_version") or "")
    if not vid or not ws:
        return None
    from ..utils import data_versions
    rec = data_versions.get_version(ws, vid)
    if rec is None:
        return None
    return dict(rec, folder_path=data_versions.version_dir(ws, rec.get("folder", "")))


def _version_referencing(ws, corrected_path):
    """The newest data version whose corrected product is `corrected_path`."""
    if not ws:
        return None
    from ..utils import data_versions
    want = os.path.abspath(corrected_path)
    for rec in reversed(data_versions.list_versions(ws)):
        path = (rec.get("corrected") or {}).get("path")
        if path and os.path.abspath(path) == want:
            return dict(rec, folder_path=data_versions.version_dir(ws, rec.get("folder", "")))
    return None


def resolve_quant_job(run_or_path, roi_name=None, open_slide=None):
    """The `QuantJob` of one region of a run, or QuantSourceError.

    `open_slide`: the slide open in the program; a run made on another slide
    is refused (None skips that check, e.g. in batch)."""
    run = run_or_path if isinstance(run_or_path, step3_masks.Run) else open_run(run_or_path)
    regions = run_regions(run)
    if not regions:
        raise QuantSourceError("the run records no region")
    if roi_name is None:
        roi_name = regions[0][0]
    bbox = dict(regions).get(roi_name)
    if bbox is None:
        raise QuantSourceError(f"the run has no region '{roi_name}'")

    # -- labels: the LabelStore only
    store = _label_store(run, roi_name)
    entry, compartment = (store.get(CELL), CELL) if store.get(CELL) else (store.get(NUCLEUS), NUCLEUS)
    if not entry or not entry.get("path"):
        raise QuantSourceError("the run's label store has neither cells nor nuclei")
    label_path = entry["path"]
    label_path = label_path if os.path.isabs(label_path) else os.path.join(run.run_dir, label_path)
    shape = (bbox[1] - bbox[0], bbox[3] - bbox[2])
    try:
        import zarr
        labels = zarr.open(label_path, mode="r")
    except Exception as exc:                                    # noqa: BLE001
        raise QuantSourceError(f"the label array {label_path} cannot be opened: {exc}")
    if str(labels.dtype) != "uint32" or tuple(labels.shape) != shape \
            or list(entry.get("shape") or []) != list(shape):
        raise QuantSourceError(f"the label array is {labels.dtype} {tuple(labels.shape)}, "
                               f"the region is uint32 {shape}")
    n_objects = int(entry.get("n_objects", -1))
    if n_objects < 0:
        raise QuantSourceError("the label store records no object count")
    nucleus_path, table_path, n_nuclei = _nuclei_of(store, compartment, shape, run.run_dir)

    # -- the slide: the run's, its workspace's, the open one
    ws = run.workspace
    manifest = _load_json(os.path.join(ws, "roi_manifest.json")) if ws else None
    if not manifest:
        raise QuantSourceError("the run is not inside an ROI workspace (no roi_manifest.json)")
    try:                                         # block A3: an unknown project schema
        read_project_schema(project_dir_of_workspace(ws))
    except ProjectSchemaError as exc:
        raise QuantSourceError(str(exc)) from exc
    slide = str(manifest.get("source_ome") or "")
    made_on = step3_masks.run_slide(run)
    if not slide or not os.path.isfile(slide):
        raise QuantSourceError(f"the workspace's slide {slide or '(unrecorded)'} is missing")
    if made_on and not step3_masks.same_slide(made_on, slide):
        raise QuantSourceError(f"the run was made on {made_on}, its workspace on {slide}")
    if open_slide and not step3_masks.same_slide(slide, open_slide):
        raise QuantSourceError(f"this result was made on another slide "
                               f"({os.path.basename(slide)})")
    names, slide_shape = _slide_channels(slide)
    y0, y1, x0, x1 = bbox
    if not (0 <= y0 < y1 <= slide_shape[0] and 0 <= x0 < x1 <= slide_shape[1]):
        raise QuantSourceError(f"region {list(bbox)} lies outside the slide {slide_shape}")

    # -- Step0's decisions: the handoff, its correction config, its product
    step0_dir = os.path.join(ws, "step0")
    handoff_path = os.path.join(step0_dir, "step0_roi_result.json")
    handoff = _load_json(handoff_path)
    if handoff is None:
        raise QuantSourceError("the workspace has no Step0 handoff (step0_roi_result.json)")
    cfg_path = handoff.get("correction_config_path") or os.path.join(step0_dir,
                                                                     "correction_config.json")
    # Block DV: a run made from a data version reads THAT version's
    # correction config and corrected product, not the workspace's current
    # ones (a later version may have changed them).
    version = _run_data_version(ws, run.meta)
    # A run made before data versions records the corrected product it read;
    # that product is kept (read-only) by the version that references it.
    recorded_corrected = str(((run.meta or {}).get("paths") or {})
                             .get("corrected_channels_zarr") or "")
    if version is None and recorded_corrected:
        version = _version_referencing(ws, recorded_corrected)
    if version is not None:
        vcfg = os.path.join(version["folder_path"], "correction_config.json")
        if os.path.isfile(vcfg):
            cfg_path = vcfg
    cfg = _load_json(cfg_path)
    if cfg is None:
        raise QuantSourceError(f"Step0's correction decisions are missing ({cfg_path})")
    decisions = {str(k): str(v).strip().lower()
                 for k, v in (cfg.get("channel_decisions") or {}).items()}
    wanted = _corrected_decisions(decisions)
    recorded = handoff.get("corrected_decisions") if version is None else None
    if recorded is not None and _corrected_decisions(recorded) != wanted:
        raise QuantSourceError(f"Step0's handoff lists corrected channels "
                               f"{_corrected_decisions(recorded)}, its correction config "
                               f"{wanted}")
    unknown = sorted(set(wanted) - set(names))
    if unknown:
        raise QuantSourceError(f"Step0 corrects {unknown}, which the slide does not have")

    zpath, root, container, offset, gshape = "", None, None, (0, 0), None
    if wanted:
        zpath = ((version or {}).get("corrected") or {}).get("path") \
            or (recorded_corrected if os.path.isdir(recorded_corrected) else "") \
            or handoff.get("corrected_zarr_path") \
            or os.path.join(step0_dir, "corrected_channels.zarr")
        try:
            import zarr
            root = zarr.open_group(zpath, mode="r")
        except Exception:                                       # noqa: BLE001
            raise QuantSourceError(f"Step0 corrects {sorted(wanted)} but its corrected "
                                   f"product {zpath} cannot be opened")
        in_product = _corrected_decisions(
            (root.attrs.get("correction_config") or {}).get("channel_decisions"))
        if in_product != wanted:
            raise QuantSourceError(f"the corrected product was made for {in_product}, "
                                   f"Step0's correction config says {wanted}")
        src = root.attrs.get("source_ome")
        if not src or not step3_masks.same_slide(src, slide):
            raise QuantSourceError(f"the corrected product was made from {src or '(unrecorded)'},"
                                   f" not {slide}")
        container, offset, gshape = _corrected_group(root, roi_name, bbox, zpath)

    channels = []
    for i, name in enumerate(names):
        decision = decisions.get(name, "original")
        if decision not in CORRECTED_METHODS:
            channels.append(ChannelSource(name=name, index=i, decision=decision, kind="raw",
                                          path=os.path.realpath(slide)))
            continue
        channels.append(_corrected_source(name, i, decision, cfg, container, offset, gshape,
                                          shape, slide_shape, zpath))
    return QuantJob(run_dir=run.run_dir, run_id=run.run_id, method=run.method, workspace=ws,
                    roi_name=roi_name, region=region_folder(roi_name), bbox=tuple(bbox),
                    compartment=compartment, label_path=os.path.realpath(label_path),
                    n_objects=n_objects, slide=os.path.realpath(slide), channels=tuple(channels),
                    nucleus_path=nucleus_path, table_path=table_path, n_nuclei=n_nuclei,
                    seam_merge=store.get("seam_merge") if isinstance(store.get("seam_merge"), dict)
                    else None,
                    step0={"handoff": os.path.realpath(handoff_path),
                           "correction_config": os.path.realpath(cfg_path),
                           "corrected_product": os.path.realpath(zpath) if zpath else None,
                           "decisions": decisions})


def _corrected_source(name, index, decision, cfg, container, offset, gshape, shape,
                      slide_shape, zpath):
    def refuse(why):
        raise QuantSourceError(f"channel {name}: Step0 decided {decision}, but {why} "
                               f"— no fallback to raw pixels; re-save Step0's correction")
    if name not in list(container.array_keys()):
        refuse(f"its corrected array is missing from {zpath}")
    arr = container[name]
    attrs = dict(arr.attrs)
    expect = tuple(gshape) if gshape is not None else tuple(slide_shape)
    if tuple(arr.shape) != expect:
        refuse(f"the corrected array is {tuple(arr.shape)}, its region {expect}")
    oy, ox = offset
    if oy + shape[0] > arr.shape[0] or ox + shape[1] > arr.shape[1]:
        refuse("the corrected array does not cover the region")
    if str(arr.dtype) != "float32":
        refuse(f"the corrected array is {arr.dtype}, not float32")
    method = str(attrs.get("correction_method", "")).strip().lower()
    if method != decision:
        refuse(f"the corrected array was made with '{method or 'an unrecorded method'}'")
    radius, sigma = resolve_effective_correction_params(cfg.get("method_params"),
                                                        cfg.get("channel_params"), name)
    param = radius if decision == "tophat" else sigma
    got = attrs.get("correction_param_value")
    try:
        same = got is not None and float(got) == float(param)
    except (TypeError, ValueError):
        same = False
    if not same:
        refuse(f"the corrected array was made with parameter {got!r}, the config says {param}")
    if attrs.get("channel_index") is not None and int(attrs["channel_index"]) != index:
        refuse(f"the corrected array is slide channel {attrs['channel_index']}, "
               f"'{name}' is channel {index}")
    identity = {k: attrs.get(k) for k in ("source_identity", "correction_method",
                                          "correction_param_name", "correction_param_value",
                                          "bg_correction_algo_version", "channel_index",
                                          "roi_name", "roi_bbox_fullres", "written_at")}
    identity.update(shape=[int(v) for v in arr.shape], dtype=str(arr.dtype))
    return ChannelSource(name=name, index=index, decision=decision, kind="corrected",
                         path=os.path.realpath(zpath), array=arr.path, offset=(oy, ox),
                         identity=identity)


# ── readers (the QuantReader boundary) ───────────────────────────────────

class TiffTileReader:
    """Raw slide channels, region by region, in the slide's own dtype.

    Tiled, one-page-per-channel, single-sample slides: each covered TIFF
    tile's bytes are read and decoded by tifffile (`page.decode`) on a thread
    pool. Anything else goes through tifffile's zarr store (`mode` says
    which)."""

    def __init__(self, slide, threads=8):
        import tifffile
        self.slide = slide
        self._tif = tifffile.TiffFile(slide)
        series = self._tif.series[0]
        level = series.levels[0]
        self.dtype = np.dtype(series.dtype)
        # OME series hold later pages as lightweight frames: full pages here
        self._pages = [pg.aspage() if hasattr(pg, "aspage") else pg for pg in level.pages]
        self.shape = tuple(int(v) for v in level.shape[-2:])
        self._local = threading.local()
        self._handles, self._handles_lock = [], threading.Lock()
        self._pool = ThreadPoolExecutor(max(1, int(threads)), thread_name_prefix="step4-read")
        self.mode = "tiff_tiles" if self._tiles_ok(series) else "tifffile_zarr"
        self._zarr = None
        if self.mode == "tifffile_zarr":
            import zarr
            z = zarr.open(level.aszarr(), mode="r")
            self._zarr = z["0"] if hasattr(z, "keys") else z

    def _tiles_ok(self, series):
        axes = str(series.axes)
        if axes[-2:] != "YX" or len(self._pages) < 1:
            return False
        n_ch = int(np.prod(series.shape[:-2])) if len(series.shape) > 2 else 1
        if len(self._pages) != n_ch:
            return False
        for p in self._pages:
            if (not getattr(p, "is_tiled", False) or int(p.samplesperpixel) != 1
                    or int(getattr(p, "tiledepth", 1)) != 1
                    or int(getattr(p, "imagedepth", 1)) != 1
                    or tuple(p.shape[-2:]) != self.shape or np.dtype(p.dtype) != self.dtype):
                return False
        return True

    def _fh(self):
        fh = getattr(self._local, "fh", None)
        if fh is None:
            fh = open(self.slide, "rb")
            self._local.fh = fh
            with self._handles_lock:
                self._handles.append(fh)
        return fh

    def _tile_job(self, page, i, y0, y1, x0, x1, out, th, tw, ntx):
        ty, tx = divmod(i, ntx)
        gy, gx = ty * th, tx * tw
        cnt = int(page.databytecounts[i])
        sy0, sy1 = max(gy, y0), min(gy + th, y1)
        sx0, sx1 = max(gx, x0), min(gx + tw, x1)
        if cnt == 0:
            out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = 0
            return
        fh = self._fh()
        fh.seek(int(page.dataoffsets[i]))
        data = fh.read(cnt)
        arr = page.decode(data, i, jpegtables=getattr(page, "jpegtables", None))[0]
        tile = np.asarray(arr).reshape(arr.shape[-3], arr.shape[-2]) \
            if arr.ndim >= 3 else np.asarray(arr)
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = tile[sy0 - gy:sy1 - gy, sx0 - gx:sx1 - gx]

    def read(self, indices, y0, y1, x0, x1):
        """(len(indices), y1 - y0, x1 - x0) array of the slide's dtype;
        coordinates in slide pixels."""
        out = np.empty((len(indices), y1 - y0, x1 - x0), self.dtype)
        if self.mode == "tifffile_zarr":
            z = self._zarr

            def one(k):
                c = indices[k]
                out[k] = z[c, y0:y1, x0:x1] if z.ndim == 3 else z[y0:y1, x0:x1]
            list(self._pool.map(one, range(len(indices))))
            return out
        jobs = []
        for k, c in enumerate(indices):
            page = self._pages[c]
            th, tw = int(page.tilelength), int(page.tilewidth)
            ntx = -(-self.shape[1] // tw)
            for ty in range(y0 // th, (y1 - 1) // th + 1):
                for tx in range(x0 // tw, (x1 - 1) // tw + 1):
                    jobs.append((page, ty * ntx + tx, out[k], th, tw, ntx))
        futures = [self._pool.submit(self._tile_job, page, i, y0, y1, x0, x1, o, th, tw, ntx)
                   for page, i, o, th, tw, ntx in jobs]
        for f in futures:
            f.result()
        return out

    def close(self):
        self._pool.shutdown(wait=True)
        for fh in self._handles:
            try:
                fh.close()
            except OSError:
                pass
        self._tif.close()


class JobReader:
    """The `QuantReader` of a `QuantJob`: labels with a one-pixel halo and
    channel batches, in REGION coordinates. Counts what it read where, so the
    provenance says what actually happened."""

    def __init__(self, job, read_threads=8):
        import zarr

        from ..sources import CorrectedZarrSource, OmeTiffSource
        self.job = job
        self.shape = job.shape
        self._labels = zarr.open(job.label_path, mode="r")
        self._nuclei = zarr.open(job.nucleus_path, mode="r") if job.nucleus_path else None
        # Block A2c 2/3: the image data through the PixelSource contract --
        # the raw slide as `OmeTiffSource` (its level-0 batch read is this
        # module's TiffTileReader), Step0's saved correction as
        # `CorrectedZarrSource` (the same strict group lookup as
        # `_corrected_group`), both in slide coordinates.
        self._raw = OmeTiffSource(job.slide, scan_threads=read_threads)
        self._corrected = None
        if any(ch.kind == "corrected" for ch in job.channels):
            zpaths = {ch.path for ch in job.channels if ch.kind == "corrected"}
            if len(zpaths) != 1:
                raise QuantSourceError(f"the corrected channels come from {sorted(zpaths)}, "
                                       "not one product")
            self._corrected = CorrectedZarrSource(zpaths.pop(), job.roi_name,
                                                  slide_shape=self._raw.level_shape(0))
        self._pool = ThreadPoolExecutor(max(1, int(read_threads)),
                                        thread_name_prefix="step4-corrected")
        self.reads = {"raw": 0, "corrected": 0}
        self._lock = threading.Lock()

    @property
    def raw_mode(self):
        return self._raw.scan_reader_mode()

    def channel_dtype(self, ch):
        return np.dtype(np.float32) if ch.kind == "corrected" else self._raw.dtype(ch.index)

    def nuclei(self, y0, y1, x0, x1):
        """uint32 nucleus ids of the tile (no ring), or None."""
        if self._nuclei is None:
            return None
        return np.ascontiguousarray(self._nuclei[y0:y1, x0:x1], dtype=np.uint32)

    def nucleus_table(self):
        """The whole nucleus -> cell table (M + 1 uint32), or None."""
        if self.job.table_path is None:
            return None
        import zarr
        return np.asarray(zarr.open(self.job.table_path, mode="r")[:], np.uint32)

    def labels(self, y0, y1, x0, x1):
        """uint32 (y1 - y0 + 2, x1 - x0 + 2): the tile and a one-pixel ring;
        outside the region the ring is background (0)."""
        H, W = self.shape
        out = np.zeros((y1 - y0 + 2, x1 - x0 + 2), np.uint32)
        ry0, ry1, rx0, rx1 = max(0, y0 - 1), min(H, y1 + 1), max(0, x0 - 1), min(W, x1 + 1)
        out[ry0 - (y0 - 1):ry1 - (y0 - 1), rx0 - (x0 - 1):rx1 - (x0 - 1)] = \
            self._labels[ry0:ry1, rx0:rx1]
        return out

    def channels(self, sources, y0, y1, x0, x1):
        """(len(sources), y1 - y0, x1 - x0): ONE dtype per call (the caller
        batches by dtype)."""
        kinds = {s.kind for s in sources}
        if len(kinds) != 1:
            raise ValueError("one batch, one source kind")
        by, bx = self.job.bbox[0], self.job.bbox[2]
        want = (y1 - y0, x1 - x0)
        if kinds == {"raw"}:
            out, _ = self._raw.read_regions([s.index for s in sources], 0,
                                            by + y0, by + y1, bx + x0, bx + x1)
            if out.shape[1:] != want:
                raise QuantSourceError(f"the slide does not cover the tile {[y0, y1, x0, x1]}")
            with self._lock:
                self.reads["raw"] += len(sources)
            return out
        out = np.empty((len(sources), y1 - y0, x1 - x0), np.float32)

        def one(k):
            arr, _ = self._corrected.read_region(sources[k].name, 0, by + y0, by + y1,
                                                 bx + x0, bx + x1)
            if arr.shape != want:                  # fail closed, never partly raw
                raise QuantSourceError(f"the corrected product does not cover the tile "
                                       f"{[y0, y1, x0, x1]} of channel {sources[k].name}")
            out[k] = arr
        list(self._pool.map(one, range(len(sources))))
        with self._lock:
            self.reads["corrected"] += len(sources)
        return out

    def close(self):
        self._pool.shutdown(wait=True)
        if self._corrected is not None:
            self._corrected.close()
        self._raw.close()
