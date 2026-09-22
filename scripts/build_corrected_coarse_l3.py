"""Explicit, offline backfill of the persisted coarse plane (G3.2b.4E).

For products written BEFORE Step0 started publishing the plane. It is a
separate command on purpose:

  * the product is opened READ ONLY and never modified;
  * nothing in the running application calls this, and the application never
    backfills by itself -- an old project simply keeps reducing at runtime;
  * the output is a sidecar store (`corrected_coarse.zarr`) beside the
    product, or wherever `--out` says.

Writing next to a real project requires `--in-place`, and even then it only
adds the sidecar directory; `corrected_channels.zarr` is not touched.

Usage:
    python scripts/build_corrected_coarse_l3.py PRODUCT.zarr --stride 64 \
        [--roi ROI_NAME] [--channels CD22,CD163] [--out DIR] [--in-place]
"""

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import time

import numpy as np

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _mod
    _spec.loader.exec_module(_mod)
sys.path.insert(0, str(_ROOT.parent))

from block01.viewer import step1_source as sources          # noqa: E402


def reduce_whole_region(array, bbox, stride, band_blocks=64):
    """The plane, in bounded row bands. Same grid, same valid rule.

    Blocks are anchored at the LEVEL-0 origin, every block is divided by the
    number of samples that actually exist inside the region, and a band is
    cut on block boundaries so no block is split across two passes.
    """
    (by0, bx0), (bh, bw) = sources.coarse_block_range(bbox, stride)
    y0, y1, x0, x1 = (int(v) for v in bbox)
    out = np.zeros((bh, bw), np.float32)
    band = stride * int(max(1, band_blocks))
    # the first band is short when the region does not start on a block
    start = y0
    while start < y1:
        stop = min(y1, (start // stride) * stride + band)
        values = np.asarray(array[start - y0:stop - y0, :], dtype=np.float32)
        h, w = values.shape
        front_y = start % stride
        pad_y = (-(front_y + h)) % stride
        front_x = x0 % stride
        pad_x = (-(front_x + w)) % stride
        padded = np.pad(values, ((front_y, pad_y), (front_x, pad_x)))
        rows = padded.shape[0] // stride
        cols = padded.shape[1] // stride
        sums = padded.reshape(rows, stride, cols, stride).sum(axis=(1, 3),
                                                              dtype=np.float64)
        row_span = np.minimum(np.arange(rows) * stride + stride, front_y + h) \
            - np.maximum(np.arange(rows) * stride, front_y)
        col_span = np.minimum(np.arange(cols) * stride + stride, front_x + w) \
            - np.maximum(np.arange(cols) * stride, front_x)
        counts = (np.maximum(row_span, 0)[:, None]
                  * np.maximum(col_span, 0)[None, :])
        block = np.zeros((rows, cols), np.float32)
        np.divide(sums, np.maximum(counts, 1), out=block, where=counts > 0,
                  casting="unsafe")
        oy = start // stride - by0
        out[oy:oy + rows, 0:cols] = np.where(counts > 0, block,
                                             out[oy:oy + rows, 0:cols])
        start = stop
    return out, (by0, bx0)


def write_plane(sidecar, group_name, channel, plane, origin, stride, level,
                product_attrs):
    import zarr
    root = zarr.open_group(str(sidecar), mode="a")
    group = root.require_group(group_name)
    ds = group.create_dataset(channel, shape=plane.shape, dtype=np.float32,
                              chunks=(512, 512), overwrite=True)
    ds[:, :] = plane
    identity = sources.coarse_plane_identity(dict(product_attrs), stride,
                                             level=level)
    for key, value in identity.items():
        ds.attrs[key] = value
    ds.attrs["block_origin"] = [int(origin[0]), int(origin[1])]
    ds.attrs["written_by"] = "build_corrected_coarse_l3"
    ds.attrs["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # LAST, always: the pixels are there before anything says they are.
    ds.attrs["complete"] = True
    return ds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("product")
    parser.add_argument("--stride", type=int, required=True)
    parser.add_argument("--level", type=int, default=None)
    parser.add_argument("--roi", default="")
    parser.add_argument("--channels", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument(
        "--stamp-product", action="store_true",
        help="WRITES to the product: gives each channel a per-write identity "
             "(`source_identity`) if it has none. A product written before "
             "Step0 started minting one cannot be matched to a plane -- "
             "Step1 refuses every plane for it -- so a backfill is useless "
             "without this. It modifies the product's attrs, never its "
             "pixels, and it must not be pointed at a real project without "
             "the owner's explicit say-so.")
    args = parser.parse_args()

    import zarr
    product = pathlib.Path(args.product)
    root = zarr.open_group(str(product),
                           mode="a" if args.stamp_product else "r")
    groups = list(root.group_keys())
    if args.roi:
        groups = [g for g in groups if g == args.roi] or groups
    if args.out:
        sidecar = pathlib.Path(args.out)
    elif args.in_place:
        sidecar = product.parent / sources.COARSE_SIDECAR_DIRNAME
    else:
        parser.error("refusing to write beside the product without --in-place; "
                     "pass --out DIR instead")
    sidecar.parent.mkdir(parents=True, exist_ok=True)

    wanted = [c.strip() for c in args.channels.split(",") if c.strip()]
    report = {"product": str(product), "sidecar": str(sidecar),
              "stride": args.stride, "channels": []}
    for group_name in groups:
        group = root[group_name]
        bbox = list(group.attrs.get("bbox_fullres") or [])
        if len(bbox) != 4:
            continue
        for channel in list(group.array_keys()):
            if wanted and channel not in wanted:
                continue
            array = group[channel]
            attrs = dict(array.attrs)
            if not str(attrs.get("source_identity") or "").strip():
                if not args.stamp_product:
                    print(f"{group_name}/{channel}: SKIPPED -- the product "
                          f"carries no per-write identity, so no plane could "
                          f"ever be matched to it (pass --stamp-product to "
                          f"add one, which WRITES to the product)")
                    report["channels"].append(
                        {"group": group_name, "channel": channel,
                         "skipped": "no source_identity on the product"})
                    continue
                import uuid
                token = uuid.uuid4().hex
                array.attrs["source_identity"] = token
                array.attrs["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                attrs = dict(array.attrs)
            started = time.perf_counter()
            plane, origin = reduce_whole_region(array, bbox, args.stride)
            build_ms = (time.perf_counter() - started) * 1000.0
            write_plane(sidecar, group_name, channel, plane, origin,
                        args.stride, args.level, attrs)
            report["channels"].append({
                "group": group_name, "channel": channel,
                "product_shape": [int(v) for v in array.shape],
                "plane_shape": [int(v) for v in plane.shape],
                "block_origin": [int(origin[0]), int(origin[1])],
                "build_ms": round(build_ms, 1)})
            print(f"{group_name}/{channel}: {array.shape} -> {plane.shape} "
                  f"in {build_ms:.0f} ms", flush=True)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
