"""G3.2b.4E phase A: what IS today's corrected coarse tile, exactly?

Before anything may be persisted, the thing being persisted has to be
defined -- not as "L3 is roughly level-0 divided by 64", but as the exact
grid, phase, edge rule, valid rule and numeric semantics the running product
produces today. This script:

  1. states the definition it derives from the production code, and then
  2. builds an INDEPENDENT candidate L3 (a reference implementation written
     from the definition, not by calling the production reducer), the way
     Step0's writer would accumulate it while writing level-0 tiles, and
  3. compares it with what the PRODUCTION path
     (`viewer.step1_source.read_tile`) returns for every tile of the
     coarsest level, including the partial right/bottom tiles, and
  4. measures size and read time against the runtime reduction.

Everything is written to /tmp. A real corrected product may be used READ
ONLY as a reference; no user project is ever written to.

Usage:
    python scripts/diagnose_step1_coarse_l3_semantics.py OUT.json [real|synthetic]
"""

import importlib.util
import json
import math
import os
import pathlib
import shutil
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

TMP = pathlib.Path("/tmp/g324e_phase_a")
TILE = 512                      # `TileGridSpec.tile_size` used by Step1
WRITER_TILE = 4096              # `WsiCorrectionWorker`'s own tile


# ── the definition, derived from the production code ──────────────────
DEFINITION = {
    "tile_rect_level_k": "y0=ty*512, y1=min(y0+512, level_shape_h) -- the "
                         "last row/col of tiles is PARTIAL "
                         "(ui/step1_viewer_host.py::_level_rect)",
    "stride": "round(raw.level_downsample(level)) -- from the OME pyramid's "
              "own level shapes, never 2**level (same function)",
    "level0_rect": "rect * stride (viewer/step1_source.py::read_tile)",
    "phase": "y0*stride % stride == 0, so a coarse tile's blocks are always "
             "aligned with the global level-0 block grid",
    "block": "mean over the VALID samples of one stride x stride block "
             "anchored at the LEVEL-0 ORIGIN (reduce_corrected)",
    "valid": "counts > 0, i.e. the block contains at least one level-0 "
             "sample inside the ROI bbox",
    "outside_roi": "values 0.0 and valid False -- NOT black; the host paints "
                   "NaN there (Step1TileProvider.read_tile)",
    "zero_pixel": "a stored 0.0 is a SAMPLE and counts (the polygon mask "
                  "writes 0 inside the bbox)",
    "dtype": "float32 out, float64 sums internally",
}


def _level_geometry(h0, w0, downsamples):
    """`level_shape` / `level_downsample` the way a real OME pyramid reports.

    Taken from the user's own slide: 52560 -> 13140 -> 3285 -> 821, i.e.
    floor division by the nominal factor, and the effective downsample of
    the coarsest level is therefore NOT exactly the nominal one
    (52560/821 = 64.02) -- which is why the product rounds it.
    """
    levels = []
    for factor in downsamples:
        levels.append((h0 // factor, w0 // factor))
    return levels


class _FakeRaw:
    """Only the geometry a corrected read needs; no pixels are read from it."""

    def __init__(self, h0, w0, factors):
        self._shapes = _level_geometry(h0, w0, factors)
        self._factors = list(factors)

    @property
    def num_levels(self):
        return len(self._shapes)

    def level_shape(self, level):
        return self._shapes[level]

    def level_downsample(self, level):
        h0 = self._shapes[0][0]
        return h0 / float(self._shapes[level][0])


def make_product(path, bbox, seed=7, poly_zero_band=0):
    """A synthetic corrected product with the shapes Step0 really writes."""
    import zarr
    y0, y1, x0, x1 = bbox
    h, w = y1 - y0, x1 - x0
    rng = np.random.default_rng(seed)
    data = (rng.random((h, w), dtype=np.float32) * 1000.0).astype(np.float32)
    # a dark band of exact zeros (what the polygon mask writes) ...
    if poly_zero_band:
        data[:poly_zero_band, :] = 0.0
        data[:, :poly_zero_band] = 0.0
    # ... and the two values a reduction must carry rather than swallow
    data[h // 2, w // 2] = np.nan
    data[h // 2 + 1, w // 2 + 1] = np.inf
    root = zarr.open_group(str(path), mode="w")
    group = root.create_group("ROI_1")
    group.attrs["bbox_fullres"] = [int(v) for v in bbox]
    group.attrs["roi_name"] = "ROI_1"
    ds = group.create_dataset("CH", shape=(h, w), dtype=np.float32,
                              chunks=(min(1024, h), min(1024, w)),
                              overwrite=True)
    ds[:, :] = data
    ds.attrs["roi_bbox_fullres"] = [int(v) for v in bbox]
    return data


def candidate_l3(data, bbox, stride, writer_tile=WRITER_TILE):
    """The L3 Step0 COULD accumulate while writing level-0 tiles.

    An independent reference: it is written from the definition above
    (mean over the valid samples of a block anchored at the level-0
    origin), accumulated tile by tile the way the writer produces pixels,
    and it never calls the production reducer.

    Returns `(mean, counts, block_origin)` over the blocks that cover the
    ROI bbox.
    """
    y0, y1, x0, x1 = bbox
    by0, bx0 = y0 // stride, x0 // stride
    by1, bx1 = -(-y1 // stride), -(-x1 // stride)
    sums = np.zeros((by1 - by0, bx1 - bx0), np.float64)
    counts = np.zeros((by1 - by0, bx1 - bx0), np.int64)
    height, width = data.shape
    for ty in range(0, height, writer_tile):
        for tx in range(0, width, writer_tile):
            tile = data[ty:ty + writer_tile, tx:tx + writer_tile]
            gy, gx = y0 + ty, x0 + tx                 # level-0 of this tile
            th, tw = tile.shape
            front_y, front_x = gy % stride, gx % stride
            pad_y = (-(front_y + th)) % stride
            pad_x = (-(front_x + tw)) % stride
            padded = np.pad(np.asarray(tile, np.float32),
                            ((front_y, pad_y), (front_x, pad_x)))
            bh = padded.shape[0] // stride
            bw = padded.shape[1] // stride
            block_sums = padded.reshape(bh, stride, bw, stride).sum(
                axis=(1, 3), dtype=np.float64)
            rows = np.minimum(np.arange(bh) * stride + stride, front_y + th) \
                - np.maximum(np.arange(bh) * stride, front_y)
            cols = np.minimum(np.arange(bw) * stride + stride, front_x + tw) \
                - np.maximum(np.arange(bw) * stride, front_x)
            rows = np.maximum(rows, 0)
            cols = np.maximum(cols, 0)
            oy = gy // stride - by0
            ox = gx // stride - bx0
            sums[oy:oy + bh, ox:ox + bw] += block_sums
            counts[oy:oy + bh, ox:ox + bw] += rows[:, None] * cols[None, :]
    mean = np.zeros(sums.shape, np.float32)
    valid = counts > 0
    np.divide(sums, np.maximum(counts, 1), out=mean, where=valid,
              casting="unsafe")
    return mean.astype(np.float32), counts, (by0, bx0)


def runtime_tiles(table, channel, raw, level):
    """Every coarse tile of `level`, exactly as the product produces them."""
    stride = max(1, int(round(raw.level_downsample(level))))
    h, w = raw.level_shape(level)
    out = {}
    for ty in range(math.ceil(h / TILE)):
        for tx in range(math.ceil(w / TILE)):
            y0, x0 = ty * TILE, tx * TILE
            rect = (y0, min(y0 + TILE, h), x0, min(x0 + TILE, w))
            values, valid = sources.read_tile(table, channel, rect,
                                              stride=stride)
            out[(ty, tx)] = (rect, values, valid)
    return out, stride


def compare(case, table, channel, raw, level, data, bbox):
    tiles, stride = runtime_tiles(table, channel, raw, level)
    t0 = time.perf_counter()
    mean, counts, origin = candidate_l3(data, bbox, stride)
    build_ms = (time.perf_counter() - t0) * 1000.0
    by0, bx0 = origin

    worst = 0.0
    worst_at = None
    n_values = 0
    n_bit_identical = 0
    valid_mismatch = 0
    nan_agree = True
    for (ty, tx), (rect, values, valid) in tiles.items():
        ry0, ry1, rx0, rx1 = rect
        out = np.zeros(values.shape, np.float32)
        ok = np.zeros(values.shape, bool)
        # the candidate's blocks that this tile covers
        sy0, sy1 = ry0 - by0, ry1 - by0
        sx0, sx1 = rx0 - bx0, rx1 - bx0
        cy0, cy1 = max(sy0, 0), min(sy1, mean.shape[0])
        cx0, cx1 = max(sx0, 0), min(sx1, mean.shape[1])
        if cy1 > cy0 and cx1 > cx0:
            out[cy0 - sy0:cy1 - sy0, cx0 - sx0:cx1 - sx0] = mean[cy0:cy1, cx0:cx1]
            ok[cy0 - sy0:cy1 - sy0, cx0 - sx0:cx1 - sx0] = counts[cy0:cy1, cx0:cx1] > 0
        if not np.array_equal(ok, valid):
            valid_mismatch += int(np.count_nonzero(ok != valid))
        both = valid & ok
        n_values += int(np.count_nonzero(both))
        a, b = values[both], out[both]
        finite = np.isfinite(a) & np.isfinite(b)
        if not np.array_equal(np.isnan(a), np.isnan(b)):
            nan_agree = False
        n_bit_identical += int(np.count_nonzero(a[finite] == b[finite]))
        if finite.any():
            diff = np.max(np.abs(a[finite] - b[finite]))
            if diff > worst:
                worst, worst_at = float(diff), [int(ty), int(tx)]
    return {
        "case": case,
        "level": level, "stride": stride,
        "level_shape": list(raw.level_shape(level)),
        "n_tiles": len(tiles),
        "partial_tiles": sum(
            1 for (_k, (rect, _v, _q)) in tiles.items()
            if (rect[1] - rect[0]) != TILE or (rect[3] - rect[2]) != TILE),
        "candidate_shape": list(mean.shape),
        "candidate_block_origin": [int(by0), int(bx0)],
        "candidate_build_ms": round(build_ms, 2),
        "valid_mismatched_cells": valid_mismatch,
        "nan_pattern_agrees": bool(nan_agree),
        "compared_values": n_values,
        "bit_identical": n_bit_identical,
        "bit_identical_pct": (round(100.0 * n_bit_identical / n_values, 4)
                              if n_values else None),
        "max_abs_diff": worst,
        "max_abs_diff_tile": worst_at,
    }


def main():
    out_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                            else "/tmp/g324e_phase_a.json")
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    report = {"block": "G3.2b.4E phase A (semantics + candidate equivalence)",
              "definition": DEFINITION, "cases": []}

    # Level-0 geometry of the user's own slide, and its real pyramid factors.
    cases = [
        # (name, slide h0,w0, pyramid factors, ROI bbox, zero band)
        ("roi_unaligned_origin", 59040, 35520, (1, 4, 16, 64),
         (31328, 31328 + 8192, 3104, 3104 + 6144), 0),
        ("roi_aligned_origin", 59040, 35520, (1, 4, 16, 64),
         (0, 8192, 0, 6144), 0),
        ("roi_non_divisible_edges", 59040, 35520, (1, 4, 16, 64),
         (31330, 31330 + 8191, 3105, 3105 + 6141), 0),
        ("roi_with_zero_band", 59040, 35520, (1, 4, 16, 64),
         (31328, 31328 + 8192, 3104, 3104 + 6144), 300),
        ("pyramid_factor_two", 59040, 35520, (1, 2, 4, 8),
         (31328, 31328 + 8192, 3104, 3104 + 6144), 0),
    ]
    for name, h0, w0, factors, bbox, band in cases:
        path = TMP / f"{name}.zarr"
        data = make_product(path, bbox, poly_zero_band=band)
        table = sources.Step1SourceTable(
            decisions={"CH": "cucim"}, corrected_zarr_path=str(path),
            roi_name="ROI_1", roi_bbox=list(bbox), handoff_revision="")
        raw = _FakeRaw(h0, w0, factors)
        level = raw.num_levels - 1
        result = compare(name, table, "CH", raw, level, data, bbox)
        result["pyramid_factors"] = list(factors)
        result["roi_bbox"] = list(bbox)
        result["roi_shape"] = list(data.shape)
        report["cases"].append(result)
        print(f"{name:26s} stride={result['stride']:3d} "
              f"tiles={result['n_tiles']:3d} partial={result['partial_tiles']:3d} "
              f"valid_mismatch={result['valid_mismatched_cells']:6d} "
              f"bit_identical={result['bit_identical_pct']}% "
              f"max|Δ|={result['max_abs_diff']:.3e}", flush=True)

    out_path.write_text(json.dumps(report, indent=1, default=str))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
