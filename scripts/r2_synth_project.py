"""v16_5 R2: a synthetic Step1 project on disk, for the pixel comparison.

    r2_synth_project.py OUT_DIR [--dtype uint8|uint16|float32] [--roi y0,y1,x0,x1]
                        [--polygon x,y;x,y;...] [--extra K] [--extra-raw K]
                        -> one JSON line (the paths)

Writes OUT_DIR/slide.ome.tif (6 channels a..f, 3000 x 2500, three levels,
256-px blocks, `--dtype`), OUT_DIR/step0/corrected_channels.zarr (group
ROI_1, the region `--roi`) and OUT_DIR/step0/corrected_coarse.zarr, and
prints the decisions and the region:

  a  original                         raw
  b  cucim, values whole numbers 0..255 (as float32) with NaN, +-inf and
     negative patches -- whole numbers sum exactly in float64 in any order,
     so both paths reduce coarse levels to the same bits (R2 plan §3)
  c  tophat, whole numbers, a VALID sidecar plane at the coarsest stride
  d  cucim, no product                missing
  e  cucim, whole numbers, a STALE sidecar (another write's token)
  f  original                         raw

`--extra K` adds K more corrected channels x0..x{K-1} (whole numbers, no
sidecar), to fill the float family (the sampler-capacity test); `--extra-raw K` adds
K more raw channels r0..r{K-1} (the format-switch test).
`--polygon` is stored with the region (Step0 writes the rectangle as the
polygon's bounding box; the points must span it).
"""

import argparse
import json
import os
import sys

import numpy as np

H0, W0 = 3000, 2500
NAMES = ["a", "b", "c", "d", "e", "f"]
DECISIONS = {"a": "original", "b": "cucim", "c": "tophat", "d": "cucim", "e": "cucim", "f": "original"}


def raw_slide(path, dtype, names):
    import tifffile
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:H0, 0:W0]
    planes = []
    for k in range(len(names)):
        base = ((yy // 9 + xx // 7 + k * 37) % 200).astype(np.float64)
        noise = rng.integers(0, 40, (H0, W0))
        planes.append(base + noise)
    data = np.stack(planes)
    if np.dtype(dtype).kind == "f":
        data = data.astype(dtype) * 3.5 - 50.0
        data[0, 100:140, 100:180] = np.nan
    elif dtype == "uint16":
        data = (data * 230).astype(dtype)
    else:
        data = data.astype(dtype)
    with tifffile.TiffWriter(str(path), ome=True) as tw:
        tw.write(data, subifds=2, tile=(256, 256), compression="zlib",
                 metadata={"axes": "CYX", "Channel": {"Name": names}})
        cur = data
        for _ in range(2):
            cur = np.ascontiguousarray(cur[:, ::2, ::2])
            tw.write(cur, subfiletype=1, tile=(256, 256), compression="zlib")


def product_attrs(name, bbox, shape, token):
    return {"channel_name": name, "correction_method": DECISIONS.get(name, "cucim"),
            "correction_param_name": "cucim_sigma", "correction_param_value": 50,
            "bg_correction_algo_version": "7", "roi_name": "ROI_1",
            "roi_bbox_fullres": [int(v) for v in bbox], "source_shape": [int(v) for v in shape],
            "source_identity": token, "dtype": "float32"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--dtype", default="uint8")
    ap.add_argument("--roi", default="")
    ap.add_argument("--polygon", default="")
    ap.add_argument("--extra", type=int, default=0)
    ap.add_argument("--extra-raw", type=int, default=0)
    args = ap.parse_args()
    extra = [f"x{i}" for i in range(args.extra)]
    extra_raw = [f"r{i}" for i in range(args.extra_raw)]
    names = NAMES + extra + extra_raw
    decisions = dict(DECISIONS, **{n: "cucim" for n in extra}, **{n: "original" for n in extra_raw})
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:0] = [os.path.dirname(os.path.dirname(here)), os.path.dirname(here)]
    import conftest  # noqa: F401
    import zarr
    from block01.viewer import step1_source as sources

    os.makedirs(os.path.join(args.out, "step0"), exist_ok=True)
    slide = os.path.join(args.out, "slide.ome.tif")
    raw_slide(slide, args.dtype, names)
    bbox = [int(v) for v in args.roi.split(",")] if args.roi else [0, H0, 0, W0]
    y0, y1, x0, x1 = bbox
    shape = (y1 - y0, x1 - x0)
    zpath = os.path.join(args.out, "step0", "corrected_channels.zarr")
    group = zarr.open_group(zpath, mode="w").require_group("ROI_1")
    rng = np.random.default_rng(5)
    attrs = {}
    for name in ["b", "c", "e"] + extra:
        values = rng.integers(0, 256, size=shape).astype(np.float32)
        if name == "b":
            values[60:90, 50:200] = np.nan
            values[400:404, 300:600] = np.inf
            values[700:704, 100:400] = -np.inf
            values[900:1000, 900:1000] = -values[900:1000, 900:1000]
        arr = group.create_dataset(name, shape=shape, chunks=(1024, 1024), dtype=np.float32)
        arr[:, :] = values
        attrs[name] = product_attrs(name, bbox, shape, f"token-{name}")
        attrs[name]["correction_method"] = decisions[name]
        arr.attrs.update(attrs[name])
    # sidecars: c valid at the coarsest stride (4), e stale
    side = zarr.open_group(os.path.join(args.out, "step0", "corrected_coarse.zarr"), mode="w").require_group("ROI_1")
    table = sources.Step1SourceTable(decisions=decisions, corrected_zarr_path=zpath,
                                     roi_name="ROI_1", roi_bbox=bbox)
    stride = 4
    for name, stale in (("c", False), ("e", True)):
        origin, bshape = sources.coarse_block_range(bbox, stride)
        rect = (origin[0] * stride, (origin[0] + bshape[0]) * stride,
                origin[1] * stride, (origin[1] + bshape[1]) * stride)
        mean, _valid = sources.reduce_corrected(table.region(name), rect, stride)
        plane = side.create_dataset(name, shape=bshape, chunks=(256, 256), dtype=np.float32)
        plane[:, :] = mean
        ident = sources.coarse_plane_identity(attrs[name], stride, level=2)
        if stale:
            ident["source_identity"] = "an-earlier-write"
        plane.attrs.update(ident)
        plane.attrs["block_origin"] = [int(origin[0]), int(origin[1])]
        plane.attrs["complete"] = True
    polygon = [[float(v) for v in p.split(",")] for p in args.polygon.split(";") if p]
    out = {"slide": slide, "zarr": zpath, "decisions": decisions, "roi": bbox,
           "polygon": polygon or None, "names": names}
    with open(os.path.join(args.out, "project.json"), "w") as fh:
        json.dump(out, fh)
    print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
