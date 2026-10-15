"""v16_5 R7b B2.3: offline backfill of a corrected product's display layers
(`viewer/display_pyramid.py`) for projects written before Step0 made them.

  * the product is opened READ ONLY; nothing is ever written to it (there is
    no `--stamp-product`: a channel without `source_identity` is skipped);
  * output: `corrected_display.zarr` beside the product only with
    `--in-place` (and the project owner's say-so), else `--out DIR`, which
    Step1 never reads (it only looks beside the product);
  * strides: the slide's pyramid (`--slide`, default the product's
    `source_ome`), or `--strides 2,4,8`;
  * read in full-width bands of whole chunk rows, bounded by `--max-bytes`.

Usage:
    python scripts/build_corrected_display.py PRODUCT.zarr (--in-place | --out DIR)
        [--roi GROUP] [--channels A,B] [--slide SLIDE] [--strides 2,4] [--max-bytes N]
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

from block01.viewer import display_pyramid as dp            # noqa: E402


def du(path):
    total, files = 0, 0
    for folder, _dirs, names in os.walk(path):
        for name in names:
            total += os.path.getsize(os.path.join(folder, name))
            files += 1
    return total, files


def build_channel(array, group_name, channel, out_root, strides, max_bytes):
    attrs = dict(array.attrs)
    bbox = [int(v) for v in attrs.get("roi_bbox_fullres") or []]
    if not str(attrs.get("source_identity") or "").strip():
        return {"skipped": "no source_identity on the product"}
    if len(bbox) != 4 or tuple(array.shape) != (bbox[1] - bbox[0], bbox[3] - bbox[2]):
        return {"skipped": "the product's shape is not its region's"}
    builder = dp.DisplayBuilder(out_root, group_name, channel, bbox, strides, max_bytes=max_bytes)
    height, width = (int(v) for v in array.shape)
    chunk = int(array.chunks[0])
    rows = max(chunk, (max_bytes // (width * 4 * 4)) // chunk * chunk)
    started = time.perf_counter()
    for r0 in range(0, height, rows):
        if builder.refused:
            break
        values = np.asarray(array[r0:min(height, r0 + rows), :], dtype=np.float32)
        builder.add(values, bbox[0] + r0, bbox[2])
    ok = builder.finish(attrs)
    out = {"published": ok, "seconds": round(time.perf_counter() - started, 1),
           "band_rows": rows, "strides": strides}
    if not ok:
        out["refused"] = builder.refused
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("product")
    ap.add_argument("--roi", default="")
    ap.add_argument("--channels", default="")
    ap.add_argument("--slide", default="")
    ap.add_argument("--strides", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("--max-bytes", type=int, default=dp.MAX_WORK_BYTES)
    args = ap.parse_args()

    import zarr
    product = pathlib.Path(args.product)
    root = zarr.open_group(str(product), mode="r")
    if args.out:
        out_root = pathlib.Path(args.out)
        # never the product itself, nothing inside it, and never the place
        # Step1 reads beside it (that one only with --in-place)
        out_abs, prod_abs = out_root.resolve(), product.resolve()
        beside = pathlib.Path(dp.display_root(product)).resolve()
        if out_abs == prod_abs or prod_abs in out_abs.parents or out_abs in prod_abs.parents \
                or out_abs == beside or beside in out_abs.parents:
            ap.error(f"refusing --out {out_root}: it is the product, inside it, contains it, "
                     f"or is the display store beside it (use --in-place for that)")
        out_root = out_abs
    elif args.in_place:
        out_root = pathlib.Path(dp.display_root(product))
    else:
        ap.error("refusing to write beside the product without --in-place; pass --out DIR instead")
    out_root.parent.mkdir(parents=True, exist_ok=True)
    if args.strides:
        strides = sorted({int(s) for s in args.strides.split(",") if s.strip()})
    else:
        strides = dp.level_strides(args.slide or root.attrs.get("source_ome", ""))
    if not strides:
        ap.error("no strides: give --slide or --strides")
    wanted = [c.strip() for c in args.channels.split(",") if c.strip()]
    groups = [g for g in root.group_keys() if not args.roi or g == args.roi]
    report = {"product": str(product), "out": str(out_root), "strides": strides, "channels": []}
    for group_name in groups:
        group = root[group_name]
        for channel in group.array_keys():
            if wanted and channel not in wanted:
                continue
            got = build_channel(group[channel], group_name, channel, str(out_root), strides,
                                args.max_bytes)
            if got.get("published"):
                got["bytes"], got["files"] = du(os.path.join(out_root, group_name, channel))
            got.update(group=group_name, channel=channel)
            report["channels"].append(got)
            print(json.dumps(got), flush=True)
    print(json.dumps(report, indent=1))
    # a channel that could not be written is a failure of the run
    return 1 if any(c.get("published") is False for c in report["channels"]) else 0


if __name__ == "__main__":
    sys.exit(main())
