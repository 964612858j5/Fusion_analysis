"""G3.2b.4A: what the corrected reduction costs, before and after.

The demo project is opened READ-ONLY, through the production opener, with the
production `CorrectedRegion` / `reduce_corrected`. Nothing here is imported by
a production module and nothing here writes to a project.

`_previous_reduce_corrected` below is the algorithm this block replaced,
re-created verbatim so both are timed in the SAME process, against the SAME
page-cache state, on the SAME product. Timing the old one from an older
checkout would compare two machine states, not two algorithms.

Honest limits, stated once:

* the OS page cache is NOT dropped (that needs root and would disturb other
  windows). The first pass over a product is reported separately as a
  "first access in this process" figure and is never called a cold DISK read.
* these are single-threaded CPU timings of one function. They are not the
  viewer's wall clock -- for that see
  `scripts/benchmark_step1_gpu_cold_landing.py`.

Usage:
    python scripts/benchmark_step1_corrected_reduction.py OUT.json [label]
"""

import importlib.util
import json
import pathlib
import statistics
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
if "block01" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "block01", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["block01"] = _module
    _spec.loader.exec_module(_module)
sys.path.insert(0, str(ROOT.parent))

from block01.utils.calibration_source import (  # noqa: E402
    open_corrected_channel_array)
from block01.viewer.step1_source import (  # noqa: E402
    MAX_REDUCTION_ELEMENTS, CorrectedRegion, reduce_corrected)

PROJECT = pathlib.Path("/sda1/Fusion/tmp/rois/full_wsi_20260813_023722_f270")
CORRECTED_ZARR = str(PROJECT / "step0/corrected_channels.zarr")
ROI_NAME = "Full WSI"
SLIDE_RECT = (0, 19480, 0, 21804)
#: level 3 is 304 x 340, so the complete coarse is ONE tile at this stride.
COARSE_STRIDE = 64
CORRECTED_CHANNELS = ("CD22", "CD4", "PD1", "FOXP3")
FINE_TILES = [(5000 + 512 * (k % 4), 6000 + 512 * (k // 4)) for k in range(12)]


def _previous_reduce_corrected(region, rect, stride,
                               max_elements=MAX_REDUCTION_ELEMENTS):
    """The algorithm G3.2b.4A replaced: one scatter-add per level-0 pixel."""
    y0, y1, x0, x1 = (int(v) for v in rect)
    stride = max(1, int(stride))
    py, px = y0 % stride, x0 % stride
    out_h = -(-(py + max(0, y1 - y0)) // stride)
    out_w = -(-(px + max(0, x1 - x0)) // stride)
    sums = np.zeros((out_h, out_w), np.float64)
    counts = np.zeros((out_h, out_w), np.int64)
    if region is not None and out_h and out_w:
        by0, by1, bx0, bx1 = region.bbox
        oy0, oy1 = max(y0, by0), min(y1, by1)
        ox0, ox1 = max(x0, bx0), min(x1, bx1)
        if oy1 > oy0 and ox1 > ox0:
            budget = max(1, int(max_elements))
            band = max(stride, int(np.sqrt(budget)) // stride * stride)
            rows = max(stride, (budget // band) // stride * stride)
            cursor_y = oy0
            while cursor_y < oy1:
                stop_y = min(oy1, cursor_y + rows)
                cursor_x = ox0
                while cursor_x < ox1:
                    stop_x = min(ox1, cursor_x + band)
                    slab, placed = region.read(cursor_y, stop_y,
                                               cursor_x, stop_x)
                    cursor_x = stop_x
                    if slab is None:
                        continue
                    sy0, sy1, sx0, sx1 = placed
                    block_y = (np.arange(sy0, sy1) - y0 + py) // stride
                    block_x = (np.arange(sx0, sx1) - x0 + px) // stride
                    flat = (block_y[:, None] * out_w + block_x[None, :]).ravel()
                    np.add.at(sums.ravel(), flat,
                              slab.ravel().astype(np.float64))
                    np.add.at(counts.ravel(), flat, 1)
                cursor_y = stop_y
    valid = counts > 0
    mean = np.where(valid, sums / np.maximum(counts, 1), 0.0).astype(np.float32)
    return mean, valid


def _open(channel):
    array = open_corrected_channel_array(CORRECTED_ZARR, channel, ROI_NAME)
    if array is None:
        raise SystemExit(f"no corrected product for {channel}")
    return CorrectedRegion(array, array.attrs["roi_bbox_fullres"])


def _timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - start) * 1000.0


def _summary(values):
    ordered = sorted(values)
    return {"each_ms": [round(v, 2) for v in values],
            "median_ms": round(statistics.median(ordered), 2),
            "min_ms": round(ordered[0], 2), "max_ms": round(ordered[-1], 2)}


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "corrected_reduction.json")
    label = sys.argv[2] if len(sys.argv) > 2 else "g324a"
    report = {
        "label": label,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project": str(PROJECT), "corrected_zarr": CORRECTED_ZARR,
        "slide_rect": list(SLIDE_RECT), "coarse_stride": COARSE_STRIDE,
        "max_reduction_elements": MAX_REDUCTION_ELEMENTS,
        "honesty": [
            "the OS page cache was NOT dropped; no figure here is a cold disk read",
            "both algorithms are timed in one process against one page-cache state",
            "single-threaded CPU time for one function, not the viewer's wall clock",
            "the demo project is opened read-only",
        ],
    }

    region = _open("CD22")

    # First access in THIS PROCESS, once each, reported on its own.
    (_after0, _ok0), first_after = _timed(reduce_corrected, region,
                                          SLIDE_RECT, COARSE_STRIDE)
    report["cd22_first_access_in_process_after_ms"] = round(first_after, 1)

    # Complete coarse, five repeats each, warm.
    after, before = [], []
    for _ in range(5):
        (values_after, valid_after), ms = _timed(
            reduce_corrected, region, SLIDE_RECT, COARSE_STRIDE)
        after.append(ms)
    for _ in range(5):
        (values_before, valid_before), ms = _timed(
            _previous_reduce_corrected, region, SLIDE_RECT, COARSE_STRIDE)
        before.append(ms)
    report["cd22_complete_coarse"] = {"before": _summary(before),
                                      "after": _summary(after)}
    report["cd22_complete_coarse"]["speedup"] = round(
        statistics.median(before) / statistics.median(after), 2)

    # Are they the same numbers?
    same_valid = bool((valid_before == valid_after).all())
    diff = np.abs(values_before[valid_before] - values_after[valid_before])
    report["cd22_complete_coarse_equivalence"] = {
        "valid_mask_identical": same_valid,
        "valid_blocks": int(valid_before.sum()),
        "max_abs_diff": float(diff.max()) if diff.size else 0.0,
        "exactly_equal_fraction": float(
            (values_before[valid_before] == values_after[valid_before]).mean())
        if diff.size else 1.0,
        "within_rtol_1e-6_atol_1e-5": bool(np.allclose(
            values_before[valid_before], values_after[valid_before],
            rtol=1e-6, atol=1e-5)),
    }

    # stride == 1, one current-viewport tile at a time.
    fine_after, fine_before = [], []
    for y0, x0 in FINE_TILES:
        rect = (y0, y0 + 512, x0, x0 + 512)
        (va, _), ms = _timed(reduce_corrected, region, rect, 1)
        fine_after.append(ms)
        (vb, _), ms = _timed(_previous_reduce_corrected, region, rect, 1)
        fine_before.append(ms)
        assert np.array_equal(va, vb), f"stride 1 changed at {rect}"
    report["cd22_stride1_tiles"] = {"before": _summary(fine_before),
                                    "after": _summary(fine_after)}
    report["cd22_stride1_tiles"]["speedup"] = round(
        statistics.median(fine_before) / statistics.median(fine_after), 2)

    # Four cuCIM channels' complete coarse, the demo's own overlay.
    four = {"before": {}, "after": {}}
    total_after = total_before = 0.0
    for channel in CORRECTED_CHANNELS:
        channel_region = _open(channel)
        _r, ms = _timed(reduce_corrected, channel_region,
                        SLIDE_RECT, COARSE_STRIDE)
        four["after"][channel] = round(ms, 1)
        total_after += ms
        _r, ms = _timed(_previous_reduce_corrected, channel_region,
                        SLIDE_RECT, COARSE_STRIDE)
        four["before"][channel] = round(ms, 1)
        total_before += ms
    four["total_before_ms"] = round(total_before, 1)
    four["total_after_ms"] = round(total_after, 1)
    four["speedup"] = round(total_before / total_after, 2)
    report["four_corrected_channels_complete_coarse"] = four

    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items()
                      if k.startswith(("cd22", "four"))}, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
