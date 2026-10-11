"""v16_5 R1 (plan §7 gate 3): Rust's Step1 tiles against Python's, on a
real project.

    r1_parity_probe.py --slide OME --zarr CORRECTED_ZARR --decisions JSON
                       [--roi-name NAME] [--roi y0,y1,x0,x1] [--per-level 200]
                       [--tile 512] [--channels a,b,...]   -> one JSON line
    r1_parity_probe.py --slide OME --raw-blocks 2000       -> raw blocks only

`--raw-blocks N`: N random file blocks per level, any channel, through
`RawReader` against the Python direct path, byte for byte (plan §7 gate 1).

For every pyramid level, `--per-level` random tiles plus the tiles on the
ROI's edges, per channel: `Step1TileProvider.read_tile_native` (Python,
the oracle) against `Step1Reader.read_tile` (Rust). Integers must be
equal; floats within |a-b| <= 1e-6 + 1e-6*|b|, NaN in the same places;
validity (rectangle or finite mask) equal. Read only: the project is not
written.
"""

import argparse
import json
import os
import sys
import time
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import conftest  # noqa: E402,F401  -- `block01` = this tree

from block01.ui.step1_core_adapter import build_corrected_manifest, build_raw_manifest  # noqa: E402
from block01.ui.step1_viewer_host import Step1TileProvider  # noqa: E402
from block01.viewer import native_tile as nt  # noqa: E402
from block01.viewer.raw_tile_provider import RawTileProvider  # noqa: E402
from block01.viewer.step1_source import Step1SourceTable  # noqa: E402

import fusion_viewer_core as core  # noqa: E402

RTOL = ATOL = 1e-6


def tile_of(level, ty, tx, size):
    return types.SimpleNamespace(level=level, ty=ty, tx=tx, grid=types.SimpleNamespace(tile_size=size))


def python_tile(provider, channel, tile):
    native, _ms = provider.read_tile_native(channel, tile)
    values = np.asarray(native.values)
    if native.valid_rect is None:
        mask = np.zeros(values.shape, bool)
    elif native.valid_rect == nt.FINITE:
        mask = np.isfinite(values)
    else:
        y0, y1, x0, x1 = native.valid_rect
        mask = np.zeros(values.shape, bool)
        mask[y0:y1, x0:x1] = True
    return values, mask


def rust_tile(reader, provider, channel, tile):
    rect, stride = provider._level_rect(tile)
    t = reader.read_tile(channel, tile.level, *rect, stride, tile.grid.tile_size)
    values = np.frombuffer(t["data"], dtype=np.dtype(t["dtype"]).newbyteorder("<")).reshape(t["rows"], t["cols"])
    if t["valid"] == "nothing":
        mask = np.zeros(values.shape, bool)
    elif t["valid"] == "finite":
        mask = np.isfinite(values)
    else:
        y0, y1, x0, x1 = t["rect"]
        mask = np.zeros(values.shape, bool)
        mask[y0:y1, x0:x1] = True
    return values, mask


def compare(a, b):
    """None if equal under the contract, else why."""
    va, ma = a
    vb, mb = b
    if va.shape != vb.shape:
        return f"shape {va.shape} vs {vb.shape}"
    if not np.array_equal(ma, mb):
        return f"validity differs at {int((ma != mb).sum())} px"
    if va.dtype.kind in "ui" or vb.dtype.kind in "ui":
        if va.dtype != vb.dtype:
            return f"dtype {va.dtype} vs {vb.dtype}"
        return None if np.array_equal(va, vb) else f"{int((va != vb).sum())} values differ"
    fa, fb = va.astype(np.float64), vb.astype(np.float64)
    if not np.array_equal(np.isnan(fa), np.isnan(fb)):
        return "NaN positions differ"
    both = ~np.isnan(fa)
    err = np.abs(fa[both] - fb[both])
    bad = err > ATOL + RTOL * np.abs(fb[both])
    return None if not bad.any() else f"{int(bad.sum())} values past tolerance (max {err.max():.3g})"


def edge_tiles(provider, level, roi, size):
    if roi is None:
        return []
    stride = max(1, int(round(provider.level_downsample(level))))
    h, w = provider.level_shape(level)
    ny, nx = -(-h // size), -(-w // size)
    y0, y1, x0, x1 = roi
    rows = {min(ny - 1, (y0 // stride) // size), min(ny - 1, max(0, (-(-y1 // stride) - 1)) // size)}
    cols = {min(nx - 1, (x0 // stride) // size), min(nx - 1, max(0, (-(-x1 // stride) - 1)) // size)}
    return [(ty, tx) for ty in rows for tx in range(nx)] + [(ty, tx) for tx in cols for ty in range(ny)]


def raw_blocks(slide, per_level, seed):
    provider = RawTileProvider(slide)
    reader = core.RawReader(build_raw_manifest(provider))
    rng = np.random.default_rng(seed)
    out = {"slide": slide, "levels": provider.num_levels, "blocks": 0, "mismatches": [], "errors": []}
    for level in range(provider.num_levels):
        plan = provider._direct_plan(level)
        th, tw = plan.tile if plan is not None else (512, 512)
        h, w = provider.level_shape(level)
        for _ in range(per_level):
            c = int(rng.integers(provider.num_channels))
            ty, tx = int(rng.integers(-(-h // th))), int(rng.integers(-(-w // tw)))
            y0, x0 = ty * th, tx * tw
            y1, x1 = min(h, y0 + th), min(w, x0 + tw)
            try:
                want, _ = provider.read_region(c, level, y0, y1, x0, x1)
            except Exception as exc:                     # noqa: BLE001 -- reported
                out["errors"].append({"level": level, "c": c, "ty": ty, "tx": tx, "python": repr(exc)[:80]})
                continue
            got = reader.read_region(c, level, y0, y1, x0, x1)
            out["blocks"] += 1
            if got != np.ascontiguousarray(want).tobytes():
                out["mismatches"].append({"level": level, "c": c, "ty": ty, "tx": tx})
    out["ok"] = not out["mismatches"]
    out["errors"] = out["errors"][:10]
    provider.close()
    print(json.dumps(out), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slide", required=True)
    ap.add_argument("--zarr", default="")
    ap.add_argument("--decisions", default="{}")
    ap.add_argument("--roi-name", default="")
    ap.add_argument("--roi", default="")
    ap.add_argument("--per-level", type=int, default=200)
    ap.add_argument("--tile", type=int, default=512)
    ap.add_argument("--channels", default="")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--raw-blocks", type=int, default=0)
    args = ap.parse_args()
    if args.raw_blocks:
        return raw_blocks(args.slide, args.raw_blocks, args.seed)
    roi = [int(v) for v in args.roi.split(",")] if args.roi else None
    raw = RawTileProvider(args.slide)
    table = Step1SourceTable(decisions=json.loads(args.decisions), corrected_zarr_path=args.zarr,
                             roi_name=args.roi_name, roi_bbox=roi)
    provider = Step1TileProvider(raw, table)
    strides = [max(1, int(round(raw.level_downsample(l)))) for l in range(raw.num_levels)]
    reader = core.Step1Reader(build_raw_manifest(raw), build_corrected_manifest(table, raw, strides))
    channels = args.channels.split(",") if args.channels else raw.channel_names
    rng = np.random.default_rng(args.seed)
    out = {"slide": args.slide, "levels": raw.num_levels, "channels": len(channels), "tiles": 0,
           "mismatches": [], "by_source": {}, "py_s": 0.0, "rust_s": 0.0}
    for level in range(raw.num_levels):
        h, w = raw.level_shape(level)
        ny, nx = -(-h // args.tile), -(-w // args.tile)
        picks = {(int(rng.integers(ny)), int(rng.integers(nx))) for _ in range(args.per_level)}
        picks |= set(edge_tiles(raw, level, roi, args.tile))
        for ty, tx in sorted(picks):
            channel = channels[int(rng.integers(len(channels)))]
            tile = tile_of(level, ty, tx, args.tile)
            t0 = time.perf_counter()
            py = python_tile(provider, channel, tile)
            t1 = time.perf_counter()
            rs = rust_tile(reader, provider, channel, tile)
            t2 = time.perf_counter()
            out["py_s"] += t1 - t0
            out["rust_s"] += t2 - t1
            source = table.source_of(channel)
            out["by_source"][source] = out["by_source"].get(source, 0) + 1
            out["tiles"] += 1
            why = compare(py, rs)
            if why:
                out["mismatches"].append({"level": level, "ty": ty, "tx": tx, "channel": channel,
                                          "source": source, "why": why})
    out["py_s"], out["rust_s"] = round(out["py_s"], 2), round(out["rust_s"], 2)
    out["ok"] = not out["mismatches"]
    out["mismatches"] = out["mismatches"][:20]
    raw.close()
    print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
