"""Step4's SpatialData export (docs/v17_spatialdata_export_plan.md).

Runs under a Python that has spatialdata (Step5's environment), NOT under
Block01's: `<python> -I export_spatialdata.py JOB.json`. It imports nothing
from Block01; the worker puts everything it needs into the job:

  {"h5ad": the finished (.partial) h5ad, "out": the zarr to write,
   "primary": {"name", "path", "instance_key"}, "secondary": [{"name", "path"}],
   "bbox": [y0, y1, x0, x1] (slide pixels), "pixel_um": [y, x] or null,
   "shapes": [{"kind", "uid", "position_key", "name", "run", "rings": [[[x, y], ...]]}],
   "uns": {extra uns entries}, "min_side": smallest pyramid side}

Writes one SpatialData: labels (multiscale, translated by the bbox origin
into `global` = slide pixels; `micrometers` when the pixel size is known),
shapes `regions` (when any), table `table` annotating the primary labels.
Prints one JSON line: {"ok": true, ...} or {"ok": false, "error": ...}.
"""
import json
import os
import resource
import sys
import time

import numpy as np


def _rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def centroids_from_labels(z, ids, origin):
    """(x, y) per id: the mean pixel position of each label in the mask
    `z` (a zarr array), plus `origin` (y0, x0) -- Step4's centroid. Read
    one zarr chunk at a time."""
    ids = np.asarray(ids, np.int64)
    sy = np.zeros(ids.size)
    sx = np.zeros(ids.size)
    cnt = np.zeros(ids.size)
    order = np.argsort(ids)
    sorted_ids = ids[order]
    ch, cw = z.chunks
    h, w = z.shape
    for y0 in range(0, h, ch):
        for x0 in range(0, w, cw):
            blk = np.asarray(z[y0:y0 + ch, x0:x0 + cw])
            labs, inv = np.unique(blk, return_inverse=True)
            inv = inv.reshape(blk.shape)
            yy, xx = np.indices(blk.shape)
            n = np.bincount(inv.ravel(), minlength=labs.size)
            by = np.bincount(inv.ravel(), weights=(yy + y0).ravel(), minlength=labs.size)
            bx = np.bincount(inv.ravel(), weights=(xx + x0).ravel(), minlength=labs.size)
            keep = labs != 0
            labs, n, by, bx = labs[keep], n[keep], by[keep], bx[keep]
            pos = np.searchsorted(sorted_ids, labs)
            ok = (pos < sorted_ids.size) & (sorted_ids[np.minimum(pos, sorted_ids.size - 1)] == labs)
            rows = order[pos[ok]]
            cnt[rows] += n[ok]
            sy[rows] += by[ok]
            sx[rows] += bx[ok]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.column_stack([sx / cnt + origin[1], sy / cnt + origin[0]])


def scale_factors(h, w, min_side, most=4):
    k = 0
    while k < most and min(h, w) // (2 ** (k + 1)) >= min_side:
        k += 1
    return [2] * k


def transforms(job, kind):
    from spatialdata.transformations import Identity, Scale, Sequence, Translation
    y0, _y1, x0, _x1 = job["bbox"]
    um = job.get("pixel_um")
    if kind == "labels":
        to_global = Translation([float(y0), float(x0)], axes=("y", "x"))
        out = {"global": to_global}
        if um:
            out["micrometers"] = Sequence([to_global, Scale([float(um[0]), float(um[1])], axes=("y", "x"))])
        return out
    out = {"global": Identity()}                    # shapes: already slide pixels (x, y)
    if um:
        out["micrometers"] = Scale([float(um[1]), float(um[0])], axes=("x", "y"))
    return out


def strided_multiscale(image, scale_factors, method=None, chunks=None):
    """`multiscale_spatial_image.to_multiscale` for labels with the method
    spatialdata picks for them (DASK_IMAGE_NEAREST): level k+1 = level k's
    every f-th pixel, size rounded down -- the same pixels (tested), built
    lazily. The library's own version computes each reduced level through
    `dask_image.ndinterp.affine_transform(...).compute()`, ~11.6 GB for a
    46,800 x 26,880 mask (measured); this one is written chunk by chunk."""
    from spatial_image import to_spatial_image
    from xarray import DataTree
    levels = {"scale0": image.to_dataset(name=image.name, promote_attrs=True)}
    cur, chunk = image, image.data.chunksize
    for k, f in enumerate(scale_factors):
        f = int(f)
        h, w = cur.shape
        arr = cur.data[:h // f * f:f, :w // f * f:f].rechunk(chunk)
        cur = to_spatial_image(arr, dims=image.dims, name=image.name)
        levels[f"scale{k + 1}"] = cur.to_dataset(name=image.name, promote_attrs=True)
    return DataTree.from_dict(levels)


def labels_element(job, entry):
    import dask.array as da
    import zarr
    from spatialdata.models import Labels2DModel
    from spatialdata.models import models as sd_models
    z = zarr.open(entry["path"], mode="r")
    data = da.from_zarr(z)
    if data.dtype != np.uint32 and data.dtype.kind in "iu":
        data = data.astype(np.uint32)
    factors = scale_factors(data.shape[0], data.shape[1], int(job.get("min_side", 256)))
    library = sd_models.to_multiscale
    sd_models.to_multiscale = strided_multiscale  # parse's own transformation handling is kept
    try:
        el = Labels2DModel.parse(data, dims=("y", "x"), scale_factors=factors or None,
                                 transformations=transforms(job, "labels"))
    finally:
        sd_models.to_multiscale = library
    return el, factors


def shapes_element(job):
    import geopandas as gpd
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from shapely.validation import make_valid
    from spatialdata.models import ShapesModel
    rows, geoms, repaired = [], [], 0
    for s in job.get("shapes") or []:
        # each piece repaired on its own, then the pieces' UNION: pieces of
        # one core may overlap (Regions "Group"), and a repaired
        # MultiPolygon of overlapping parts would drop their overlap
        parts = []
        for ring in s["rings"]:
            if len(ring) < 3:
                continue
            piece = Polygon([(float(x), float(y)) for x, y in ring])
            if not piece.is_valid:
                piece = make_valid(piece)
                repaired += 1
            parts.extend(_areas(piece))
        g = unary_union(parts) if parts else None
        if g is None or g.is_empty:
            continue
        geoms.append(g)
        rows.append({k: s.get(k) for k in ("kind", "uid", "position_key", "name")}
                    | {"run": bool(s.get("run", True))})
    if not geoms:
        return None, repaired
    gdf = gpd.GeoDataFrame(rows, geometry=geoms)
    for col in ("kind", "uid", "position_key", "name"):
        gdf[col] = gdf[col].fillna("").astype(str)
    return ShapesModel.parse(gdf, transformations=transforms(job, "shapes")), repaired


def _areas(geom):
    """The polygonal parts of a geometry (a repair can add lines / points)."""
    if geom.geom_type == "Polygon":
        return [geom] if not geom.is_empty else []
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [q for g in geom.geoms for q in _areas(g)]
    return []


def table_element(job):
    import anndata as ad
    import pandas as pd
    from spatialdata.models import TableModel
    adata = ad.read_h5ad(job["h5ad"])
    uns = dict(adata.uns)
    if "X_statistic" in uns:                         # SpatialData: keys unique up to case
        uns["x_statistic"] = uns.pop("X_statistic")
    for k, v in (job.get("uns") or {}).items():
        uns[k] = v
    adata.uns = uns
    prim = job["primary"]
    key = prim["instance_key"]
    if adata.n_obs and {"centroid_x", "centroid_y"} <= set(adata.obs.columns):
        xy = adata.obs[["centroid_x", "centroid_y"]].to_numpy(np.float64)
        source = "step4"
    else:
        import zarr
        y0, _y1, x0, _x1 = job["bbox"]
        xy = (centroids_from_labels(zarr.open(prim["path"], mode="r"), adata.obs[key].to_numpy(), (y0, x0))
              if adata.n_obs else np.zeros((0, 2)))
        source = "labels"
    adata.obsm["spatial"] = np.ascontiguousarray(xy, np.float64)
    if adata.n_obs == 0:                             # an annotated table needs an observed region
        return TableModel.parse(adata), source
    adata.obs["region"] = pd.Categorical([prim["name"]] * adata.n_obs)
    adata.obs[key] = adata.obs[key].astype(np.int64)
    return TableModel.parse(adata, region=prim["name"], region_key="region", instance_key=key), source


def _shape(element):
    """Level-0 shape of a labels element (multiscale tree or one array)."""
    try:
        return tuple(int(v) for v in element["scale0"]["image"].shape)
    except (KeyError, TypeError):
        return tuple(int(v) for v in element.shape)


#: dask threads: each holds source chunks while it works; the default (one
#: per core) took 6.6 GB for a whole-slide mask, 8 took 1.7 GB at the same
#: speed (measured)
DASK_WORKERS = max(1, min(8, os.cpu_count() or 1))


def main(job_path):
    t0 = time.perf_counter()
    import dask
    dask.config.set(scheduler="threads", num_workers=DASK_WORKERS)
    import spatialdata as sd
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    labels, levels = {}, {}
    for entry in [job["primary"]] + list(job.get("secondary") or []):
        labels[entry["name"]], levels[entry["name"]] = labels_element(job, entry)
    shapes, repaired = shapes_element(job)
    n_shapes = 0 if shapes is None else int(len(shapes))
    table, centroid_source = table_element(job)
    n_obs = int(table.n_obs)
    sdata = sd.SpatialData(labels=labels, shapes={"regions": shapes} if shapes is not None else {},
                           tables={"table": table})
    sdata.write(job["out"], overwrite=False)
    del sdata, table, labels, shapes
    # verification: element names and label shapes, the table's rows (its
    # obs only -- not a second full table)
    back = sd.read_zarr(job["out"], selection=("labels", "shapes"))
    got = {name: _shape(back.labels[name]) for name in back.labels}
    import zarr
    want = {e["name"]: tuple(int(v) for v in zarr.open(e["path"], mode="r").shape)
            for e in [job["primary"]] + list(job.get("secondary") or [])}
    if got != want:
        raise RuntimeError(f"the written labels are {got}, expected {want}")
    if (shapes_written := ("regions" in back.shapes)) != (shapes_expected := n_shapes > 0):
        raise RuntimeError(f"shapes written: {shapes_written}, expected: {shapes_expected}")
    rows = zarr.open_group(os.path.join(job["out"], "tables", "table"), mode="r")
    idx = rows["obs"].attrs["_index"]
    n_back = int(rows["obs"][idx].shape[0])
    if n_back != n_obs:
        raise RuntimeError(f"the written table has {n_back} rows, expected {n_obs}")
    print(json.dumps({"ok": True, "spatialdata": sd.__version__, "n_obs": n_obs,
                      "labels": {k: {"shape": got.get(k), "scale_factors": levels[k]} for k in levels},
                      "shapes": 0 if "regions" not in back.shapes else int(len(back.shapes["regions"])),
                      "shapes_repaired": repaired, "centroids": centroid_source,
                      "seconds": round(time.perf_counter() - t0, 2), "peak_rss_mb": round(_rss_mb())}),
          flush=True)


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as exc:                         # noqa: BLE001 -- reported to the worker
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        sys.exit(1)
