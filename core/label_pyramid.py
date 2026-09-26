"""Label pyramids for a Step2 mask -- block N.

No Qt. A Step2 mask is one level (uint32 zarr, the ROI's own coordinates).
Browsing it over the whole slide (Step3, the Odon way) needs coarser levels
on the SAME grids as the slide's own pyramid, so the mask stays on the image
at every zoom.

Coordinate contract (as the viewer, `viewer/raw_tile_provider.py`
`level_downsample_yx`: geometry uses per-axis UNROUNDED ratios):
  * level L of the labels is the slide's level L grid: shape (H_L, W_L) from
    the slide, ds_y = H_0 / H_L and ds_x = W_0 / W_L, floats, per axis;
  * pixel (i, j) of level L holds the mask at the level-0 slide pixel
    (floor((i + 0.5) * ds_y), floor((j + 0.5) * ds_x)) -- nearest, at the
    pixel centre -- and 0 where that point is outside the ROI;
  * each level array covers the ROI only: rows floor(y0 / ds_y) ..
    ceil(y1 / ds_y) (exclusive), columns likewise; it is named by its level
    number ("1", "2", ...).
Level 0 is the mask itself and is never copied.

A pyramid is written under `<out>.partial` and renamed to `<out>` only after
every level and the `complete` flag are written; a cancel or a failure
removes the partial directory. `read` accepts only a complete pyramid whose
level-0 mask is still the one it was built from.
"""

import math
import os
import shutil

import numpy as np

VERSION = 1
CHUNK = 1024
SAMPLING = ("level L pixel (i, j) = the mask at level-0 slide pixel "
            "(floor((i + 0.5) * ds_y), floor((j + 0.5) * ds_x)); 0 outside the ROI")


class Cancelled(Exception):
    """The build was stopped; nothing was left behind."""


def raw_level_shapes(raw_ome_path):
    """[(H_L, W_L)] of the slide's pyramid, level 0 first."""
    import tifffile
    with tifffile.TiffFile(raw_ome_path) as tf:
        return [tuple(int(v) for v in lvl.shape[-2:]) for lvl in tf.series[0].levels]


def level_grid(raw_shapes, roi_bbox):
    """For every slide level L >= 1: its ratios and the ROI's rows / columns
    on that level's grid, as dicts {level, ds_y, ds_x, origin, shape}."""
    h0, w0 = (int(v) for v in raw_shapes[0])
    y0, y1, x0, x1 = (int(v) for v in roi_bbox)
    out = []
    for level, (hl, wl) in enumerate(raw_shapes):
        if level == 0 or hl <= 0 or wl <= 0:
            continue
        ds_y, ds_x = h0 / hl, w0 / wl
        i0, i1 = int(math.floor(y0 / ds_y)), min(int(hl), int(math.ceil(y1 / ds_y)))
        j0, j1 = int(math.floor(x0 / ds_x)), min(int(wl), int(math.ceil(x1 / ds_x)))
        out.append({"level": level, "ds_y": ds_y, "ds_x": ds_x,
                    "origin": [i0, j0], "shape": [max(0, i1 - i0), max(0, j1 - j0)]})
    return out


def _source_index(start, count, ds, roi_start, roi_len):
    """ROI-local level-0 indices sampled by `count` level pixels from `start`,
    and which of them fall inside the ROI."""
    centre = np.floor((np.arange(start, start + count) + 0.5) * ds).astype(np.int64)
    local = centre - int(roi_start)
    return local, (local >= 0) & (local < int(roi_len))


def build(level0_zarr, out_path, raw_shapes, roi_bbox, kind, cancel_check=None):
    """Write the pyramid of the mask at `level0_zarr` (ROI coordinates,
    shape = the ROI's bbox) to `out_path`; returns `out_path`. Raises
    `Cancelled` when `cancel_check()` turns true; any other failure raises
    as it is. Either way no partial directory is left."""
    import zarr
    from numcodecs import Blosc

    src = zarr.open(level0_zarr, mode="r")
    y0, y1, x0, x1 = (int(v) for v in roi_bbox)
    if tuple(src.shape) != (y1 - y0, x1 - x0):
        raise ValueError(f"the mask is {tuple(src.shape)}, the ROI's bbox is "
                         f"{(y1 - y0, x1 - x0)}")
    partial = out_path + ".partial"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        group = zarr.open_group(partial, mode="w")
        levels = level_grid(raw_shapes, roi_bbox)
        comp = Blosc(cname="lz4", clevel=5, shuffle=Blosc.SHUFFLE)
        for lv in levels:
            h, w = lv["shape"]
            arr = group.create_dataset(str(lv["level"]), shape=(h, w), chunks=(CHUNK, CHUNK),
                                       dtype="<u4", compressor=comp, fill_value=0)
            i0, j0 = lv["origin"]
            for a in range(0, h, CHUNK):
                rows, rok = _source_index(i0 + a, min(CHUNK, h - a), lv["ds_y"], y0, src.shape[0])
                for b in range(0, w, CHUNK):
                    if cancel_check is not None and cancel_check():
                        raise Cancelled()
                    cols, cok = _source_index(j0 + b, min(CHUNK, w - b), lv["ds_x"], x0,
                                              src.shape[1])
                    block = np.zeros((rows.size, cols.size), dtype=np.uint32)
                    if rok.any() and cok.any():
                        block[np.ix_(rok, cok)] = src.get_orthogonal_selection(
                            (rows[rok], cols[cok]))
                    arr[a:a + rows.size, b:b + cols.size] = block
        group.attrs.update({
            "label_pyramid_version": VERSION,
            "kind": kind,
            "level0": {"path": os.path.relpath(os.path.abspath(level0_zarr),
                                               os.path.dirname(os.path.abspath(out_path))),
                       "shape": list(src.shape)},
            "roi_bbox": [y0, y1, x0, x1],
            "raw_level_shapes": [list(s) for s in raw_shapes],
            "levels": [dict(lv, path=str(lv["level"])) for lv in levels],
            "sampling": SAMPLING,
        })
        group.attrs["complete"] = True           # last: the pyramid is whole
        if os.path.lexists(out_path):
            shutil.rmtree(out_path)
        os.replace(partial, out_path)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return out_path


def read(out_path):
    """The pyramid's attributes when it is complete and its level-0 mask is
    still there with the shape it was built from; otherwise None."""
    import zarr
    try:
        attrs = dict(zarr.open_group(out_path, mode="r").attrs)
    except Exception:  # noqa: BLE001 -- missing or not a group: no pyramid
        return None
    if attrs.get("complete") is not True or attrs.get("label_pyramid_version") != VERSION:
        return None
    level0 = attrs.get("level0") or {}
    path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(out_path)),
                                         str(level0.get("path") or "")))
    try:
        shape = list(zarr.open(path, mode="r").shape)
    except Exception:  # noqa: BLE001
        return None
    if shape != list(level0.get("shape") or []):
        return None
    attrs["level0_abs"] = path
    return attrs
