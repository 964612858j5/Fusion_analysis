"""G3.2b.4E phase A, part 2: display equivalence, size, and read time.

Three questions the semantics comparison does not answer:

  * do the two coarse planes produce the SAME PICTURE through the existing
    C1 Overlay/Fusion composition (RGBA <= 1 LSB, alpha identical)?
  * what does a persisted L3 actually cost on disk, and how long does it
    take to read back, against the runtime whole-region reduction it would
    replace?
  * does the non-finite behaviour (NaN / +-Inf inside a block) agree?

The real corrected products are opened READ ONLY; every candidate array is
written under /tmp and nowhere else. The OS page cache is NOT dropped, so
no number here is a cold DISK read -- first in-process access is labelled
as such.

Usage:
    python scripts/diagnose_step1_coarse_l3_measure.py OUT.json [n_channels]
"""

import importlib.util
import json
import math
import pathlib
import shutil
import subprocess
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

from block01.viewer import step1_compose as compose_core     # noqa: E402
from block01.viewer import step1_source as sources           # noqa: E402

sys.path.insert(0, str(_ROOT / "scripts"))
from diagnose_step1_coarse_l3_semantics import (             # noqa: E402
    TILE, _FakeRaw, candidate_l3, make_product, runtime_tiles)

TMP = pathlib.Path("/tmp/g324e_phase_a2")

#: The user's own project, READ ONLY. Two corrected channels are asked for
#: so the block's "at least two channels" requirement is met from real data.
REAL = {
    "zarr": "/nvme0n1p1/2025.12.20_Final_17209_16_Slice4/Scan1/pipeline_v2/"
            "rois/full_wsi_20260921_175811_4908/step0/corrected_channels.zarr",
    "roi_name": "Full WSI",
    "roi_bbox": [0, 59040, 0, 35520],
    "channels": ["CD22", "CD163"],
    "slide_h0": 59040, "slide_w0": 35520,
    "pyramid_factors": (1, 4, 16, 64),
}


def place(mean, counts, origin, rect, shape):
    """Put the candidate's blocks where this tile expects them.

    The tile is addressed on the WHOLE SLIDE's level-k grid while the
    candidate covers only the ROI's blocks, so the two ranges are
    intersected -- a tile north or west of the ROI gets nothing, which is
    exactly what `valid` says today.
    """
    by0, bx0 = origin
    ry0, ry1, rx0, rx1 = rect
    out = np.zeros(shape, np.float32)
    ok = np.zeros(shape, bool)
    sy0, sy1 = ry0 - by0, ry1 - by0
    sx0, sx1 = rx0 - bx0, rx1 - bx0
    cy0, cy1 = max(sy0, 0), min(sy1, mean.shape[0])
    cx0, cx1 = max(sx0, 0), min(sx1, mean.shape[1])
    if cy1 > cy0 and cx1 > cx0:
        out[cy0 - sy0:cy1 - sy0, cx0 - sx0:cx1 - sx0] = mean[cy0:cy1, cx0:cx1]
        ok[cy0 - sy0:cy1 - sy0, cx0 - sx0:cx1 - sx0] = (
            counts[cy0:cy1, cx0:cx1] > 0 if counts is not None else True)
    return out, ok


def rgba_equivalence(runtime, candidate, channels):
    """One frame of the existing Overlay and Fusion, from both planes."""
    mappings = {ch: (0.0, 1000.0, 1.0) for ch in channels}
    colors = {ch: ((1.0, 0.2, 0.2) if i else (0.2, 0.6, 1.0))
              for i, ch in enumerate(channels)}
    weights = {ch: 1.0 for ch in channels}
    groups = {"markers": {ch: 1.0 for ch in channels}}
    out = {}
    for mode, kwargs in (("overlay", dict(weights=weights, colors=colors,
                                          mappings=mappings)),
                         ("fusion", dict(groups=groups,
                                         group_weights={"markers": 1.0},
                                         nucleus=("", 0.0),
                                         mappings=mappings))):
        rgba_a, valid_a, _ = compose_core.compose(
            "fusion" if mode == "fusion" else "overlay", runtime, **kwargs)
        rgba_b, valid_b, _ = compose_core.compose(
            "fusion" if mode == "fusion" else "overlay", candidate, **kwargs)
        if rgba_a is None or rgba_b is None:
            out[mode] = {"composed": False}
            continue
        a = rgba_a.astype(np.int16)
        b = rgba_b.astype(np.int16)
        out[mode] = {
            "composed": True,
            "rgb_max_abs_diff_lsb": int(np.max(np.abs(a[..., :3] - b[..., :3]))),
            "alpha_identical": bool(np.array_equal(a[..., 3], b[..., 3])),
            "valid_identical": bool(np.array_equal(valid_a, valid_b)),
            "shape": list(rgba_a.shape),
        }
    return out


def synthetic_display_case():
    """Two channels, an unaligned ROI, zeros and non-finite values."""
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    bbox = (31328, 31328 + 4096, 3104, 3104 + 3072)
    raw = _FakeRaw(59040, 35520, (1, 4, 16, 64))
    level = raw.num_levels - 1
    runtime, candidate = {}, {}
    nonfinite = {}
    for index, channel in enumerate(("CH_A", "CH_B")):
        path = TMP / f"{channel}.zarr"
        data = make_product(path, bbox, seed=11 + index, poly_zero_band=96)
        table = sources.Step1SourceTable(
            decisions={"CH": "cucim"}, corrected_zarr_path=str(path),
            roi_name="ROI_1", roi_bbox=list(bbox), handoff_revision="")
        tiles, stride = runtime_tiles(table, "CH", raw, level)
        mean, counts, origin = candidate_l3(data, bbox, stride)
        # THE TILE THE ROI ACTUALLY FALLS IN, not tile (0,0): the ROI starts
        # at block 489 on this slide, so the north-west tile is empty.
        key = max(tiles, key=lambda k: int(np.count_nonzero(tiles[k][2])))
        rect, values, valid = tiles[key]
        cand, cand_valid = place(mean, counts, origin, rect, values.shape)
        runtime[channel] = (values, valid)
        candidate[channel] = (cand, cand_valid)
        both = valid & cand_valid
        a, b = values[both], cand[both]
        nf = ~np.isfinite(a) | ~np.isfinite(b)
        nonfinite[channel] = {
            "n_non_finite_cells": int(np.count_nonzero(nf)),
            "nan_pattern_identical": bool(np.array_equal(np.isnan(a), np.isnan(b))),
            "posinf_pattern_identical": bool(
                np.array_equal(np.isposinf(a), np.isposinf(b))),
            "neginf_pattern_identical": bool(
                np.array_equal(np.isneginf(a), np.isneginf(b))),
            "finite_cells_bit_identical": bool(
                np.array_equal(a[~nf], b[~nf])),
        }
    return {"rgba": rgba_equivalence(runtime, candidate, ["CH_A", "CH_B"]),
            "non_finite": nonfinite,
            "roi_bbox": list(bbox)}


def real_case(n_channels):
    """Real corrected products: runtime cost, candidate size, read time."""
    import zarr
    root = zarr.open_group(REAL["zarr"], mode="r")
    group = root[[k for k in root.group_keys()][0]]
    channels = [c for c in REAL["channels"] if c in group][:n_channels]
    raw = _FakeRaw(REAL["slide_h0"], REAL["slide_w0"], REAL["pyramid_factors"])
    level = raw.num_levels - 1
    stride = max(1, int(round(raw.level_downsample(level))))
    bbox = tuple(REAL["roi_bbox"])
    out = {"zarr": REAL["zarr"], "level": level, "stride": stride,
           "channels": [], "os_page_cache_dropped": False}

    for channel in channels:
        table = sources.Step1SourceTable(
            decisions={channel: "cucim"}, corrected_zarr_path=REAL["zarr"],
            roi_name=REAL["roi_name"], roi_bbox=list(bbox),
            handoff_revision="")
        assert table.source_of(channel) == "corrected", channel

        # ── today: the runtime whole-region reduction, tile by tile ──
        t0 = time.perf_counter()
        tiles, _ = runtime_tiles(table, channel, raw, level)
        runtime_ms = (time.perf_counter() - t0) * 1000.0

        # ── the candidate, built ONCE from the same product (read-only) ──
        region = table.region(channel)
        by0, bx0 = bbox[0] // stride, bbox[2] // stride
        by1, bx1 = -(-bbox[1] // stride), -(-bbox[3] // stride)
        mean = np.zeros((by1 - by0, bx1 - bx0), np.float32)
        t0 = time.perf_counter()
        # walk the product in level-0 row bands that are whole blocks
        band = stride * 64
        for gy in range(bbox[0], bbox[1], band):
            gy1 = min(gy + band, bbox[1])
            values, placed = region.read(gy, gy1, bbox[2], bbox[3])
            if values is None:
                continue
            h, w = values.shape
            pad_y = (-h) % stride
            pad_x = (-w) % stride
            padded = np.pad(np.asarray(values, np.float32),
                            ((0, pad_y), (0, pad_x)))
            bh, bw = padded.shape[0] // stride, padded.shape[1] // stride
            sums = padded.reshape(bh, stride, bw, stride).sum(axis=(1, 3),
                                                              dtype=np.float64)
            rows = np.minimum(np.arange(bh) * stride + stride, h) \
                - np.arange(bh) * stride
            cols = np.minimum(np.arange(bw) * stride + stride, w) \
                - np.arange(bw) * stride
            counts = np.maximum(rows, 0)[:, None] * np.maximum(cols, 0)[None, :]
            oy = gy // stride - by0
            block = np.zeros((bh, bw), np.float32)
            np.divide(sums, np.maximum(counts, 1), out=block,
                      where=counts > 0, casting="unsafe")
            mean[oy:oy + bh, 0:bw] = block
        build_ms = (time.perf_counter() - t0) * 1000.0

        # ── write the candidate to /tmp and measure it ──
        TMP.mkdir(parents=True, exist_ok=True)
        cand_path = TMP / f"real_{channel}_l3.zarr"
        if cand_path.exists():
            shutil.rmtree(cand_path)
        store = zarr.open_group(str(cand_path), mode="w")
        ds = store.create_dataset(channel, shape=mean.shape, dtype=np.float32,
                                  chunks=(512, 512), overwrite=True)
        ds[:, :] = mean
        size_bytes = int(subprocess.check_output(
            ["du", "-sb", str(cand_path)]).split()[0])

        # ── read it back the way a coarse tile would ──
        reread = zarr.open_group(str(cand_path), mode="r")[channel]
        h_k, w_k = raw.level_shape(level)
        t0 = time.perf_counter()
        n_tiles = 0
        for ty in range(math.ceil(h_k / TILE)):
            for tx in range(math.ceil(w_k / TILE)):
                y0, x0 = ty * TILE, tx * TILE
                y1, x1 = min(y0 + TILE, h_k), min(x0 + TILE, w_k)
                sy0, sx0 = y0 - by0, x0 - bx0
                sy1 = min(y1 - by0, reread.shape[0])
                sx1 = min(x1 - bx0, reread.shape[1])
                if sy1 > sy0 and sx1 > sx0:
                    _ = np.asarray(reread[sy0:sy1, sx0:sx1], np.float32)
                n_tiles += 1
        read_ms = (time.perf_counter() - t0) * 1000.0

        # ── and check it against the runtime tiles, cell by cell ──
        worst = 0.0
        mismatched_valid = 0
        compared = 0
        for (ty, tx), (rect, values, valid) in tiles.items():
            cand, cand_valid = place(mean, None, (by0, bx0), rect, values.shape)
            mismatched_valid += int(np.count_nonzero(cand_valid != valid))
            both = valid & cand_valid
            compared += int(np.count_nonzero(both))
            a, b = values[both], cand[both]
            finite = np.isfinite(a) & np.isfinite(b)
            if finite.any():
                worst = max(worst, float(np.max(np.abs(a[finite] - b[finite]))))

        out["channels"].append({
            "channel": channel,
            "product_shape": list(region.array.shape),
            "l3_shape": list(mean.shape),
            "l3_bytes_on_disk": size_bytes,
            "l3_bytes_per_channel_mib": round(size_bytes / (1024 * 1024), 3),
            "runtime_full_coarse_ms": round(runtime_ms, 1),
            "candidate_build_ms_readonly": round(build_ms, 1),
            "l3_read_all_tiles_ms": round(read_ms, 2),
            "n_coarse_tiles": n_tiles,
            "valid_mismatched_cells": mismatched_valid,
            "compared_cells": compared,
            "max_abs_diff": worst,
        })
        print(f"{channel:8s} runtime={runtime_ms:9.1f} ms  "
              f"l3_read={read_ms:7.2f} ms  size={size_bytes/1048576:6.2f} MiB  "
              f"valid_mismatch={mismatched_valid}  max|Δ|={worst:.3e}",
              flush=True)
    return out


def main():
    out_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                            else "/tmp/g324e_phase_a2.json")
    n_channels = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    report = {"block": "G3.2b.4E phase A part 2",
              "synthetic_display": synthetic_display_case()}
    print(json.dumps(report["synthetic_display"]["rgba"], indent=1))
    print(json.dumps(report["synthetic_display"]["non_finite"], indent=1))
    report["real"] = real_case(n_channels)
    out_path.write_text(json.dumps(report, indent=1, default=str))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
