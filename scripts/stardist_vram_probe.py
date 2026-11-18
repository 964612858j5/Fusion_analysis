"""StarDist's memory-lean network on a CUDA card: equal bit for bit, and how
much memory it needs (docs/v17_stardist_vram_tile_plan.md §4).

For each window size it runs the real 2D_versatile_fluo weights through
  * `graph.forward_reference` -- the plain graph the product ran before, and
  * `graph.forward`           -- the memory-lean graph it runs now,
under the product's numerics (`stardist_torch/numerics.py`), and reports
  * whether both heads are EQUAL (torch.equal), and the largest difference;
  * each pass's peak allocated / reserved CUDA memory and seconds;
  * the lean pass's peak bytes per input pixel (what sizes Step2's tiles).
Windows the reference cannot hold are run with the lean graph only.

The input is a DAPI window of a real slide (`--ome`, channel `--channel`),
normalised as the engine normalises (`engines.stardist_input`) and padded to a
multiple of 16 as stardist pads; without `--ome`, a synthetic nucleus image.

Usage (the Block01 environment, from the repository root):
    python scripts/stardist_vram_probe.py --ome C:\\path\\slide.ome.tif
    python scripts/stardist_vram_probe.py --json probe.json
It reads only; it writes nothing but the optional `--json` report.
"""
import argparse
import json
import pathlib
import sys
import time

import numpy as np

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

#: both graphs (the reference holds ~390 B/px: up to ~9 MP on a 6 GiB card)
IDENTITY_SIZES = [(1024, 1024), (2048, 2048), (2560, 2560), (3072, 3072)]
#: the lean graph alone, up to the tile that ran out of memory on 2026-11-18
LEAN_SIZES = [(3584, 3584), (4096, 4096), (4272, 4464), (4800, 4800)]


def _window(args, h, w):
    if not args.ome:
        rng = np.random.default_rng(1)
        yy, xx = np.mgrid[0:h, 0:w]
        img = rng.normal(400, 60, (h, w))
        for _ in range(h * w // 900):
            cy, cx, r = rng.uniform(0, h), rng.uniform(0, w), rng.uniform(4, 9)
            sl = (slice(max(0, int(cy - r)), int(cy + r) + 1), slice(max(0, int(cx - r)), int(cx + r) + 1))
            img[sl][(yy[sl] - cy) ** 2 + (xx[sl] - cx) ** 2 <= r * r] += rng.uniform(800, 3000)
        return img.astype(np.float32)
    import tifffile
    import zarr
    with tifffile.TiffFile(args.ome) as tf:
        z = zarr.open(tf.series[0].aszarr(level=0), mode="r")
        arr = z if not isinstance(z, zarr.hierarchy.Group) else z["0"]
        H, W = arr.shape[-2:]
        y0, x0 = max(0, (H - h) // 2), max(0, (W - w) // 2)
        return np.asarray(arr[args.channel, y0:y0 + h, x0:x0 + w], dtype=np.float32)


def _input(args, h, w, device):
    import torch
    from seg_runner import engines
    x = engines.stardist_input(_window(args, h, w))
    if x is None:
        raise SystemExit(f"the {h}x{w} window has no contrast; pick another --ome / --channel")
    ph, pw = (-x.shape[0]) % 16, (-x.shape[1]) % 16
    x = np.pad(x, ((0, ph), (0, pw)), mode="reflect")
    return torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32))[None, None].to(device)


def _run(fn, params, x, device):
    import torch
    from seg_runner.stardist_torch import numerics
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats(device)
    base = torch.cuda.memory_allocated(device)
    torch.cuda.synchronize(device)
    t0 = time.perf_counter()
    with numerics.numerics(device), torch.inference_mode():
        out = fn(params, x)
    torch.cuda.synchronize(device)
    took = time.perf_counter() - t0
    gib = 1024.0 ** 3
    peak = torch.cuda.max_memory_allocated(device)
    return out, {"seconds": round(took, 3), "peak_allocated_gib": round(peak / gib, 3),
                 "peak_reserved_gib": round(torch.cuda.max_memory_reserved(device) / gib, 3),
                 "bytes_per_px": round((peak - base) / float(x.shape[2] * x.shape[3]), 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ome", help="an OME-TIFF slide; its middle window is used")
    ap.add_argument("--channel", type=int, default=0, help="the DAPI channel index (default 0)")
    ap.add_argument("--json", help="write the report here as JSON")
    args = ap.parse_args()

    import torch
    if not torch.cuda.is_available():
        raise SystemExit("no CUDA device: this probe is for the CUDA card")
    from seg_runner import engines
    from seg_runner.stardist_torch import graph, weights
    device = "cuda"
    entry = engines.manifest_entry(engines.STARDIST_TORCH_ENTRY)
    path = pathlib.Path(engines.model_path(engines.STARDIST_TORCH_ENTRY)) / weights.WEIGHTS_FILE
    params = weights.load(str(path), device, entry.get("content_sha256"))["tensors"]
    props = torch.cuda.get_device_properties(0)
    report = {"device": props.name, "total_gib": round(props.total_memory / 1024.0 ** 3, 2),
              "torch": torch.__version__, "cudnn": torch.backends.cudnn.version(),
              "input": args.ome or "synthetic", "windows": []}
    print(f"{props.name}, {report['total_gib']} GiB, torch {torch.__version__}, cuDNN {report['cudnn']}")

    for h, w in IDENTITY_SIZES + LEAN_SIZES:
        x = _input(args, h, w, device)
        row = {"window": [h, w], "padded": list(x.shape[2:])}
        try:
            lean, row["lean"] = _run(graph.forward, params, x, device)
        except torch.cuda.OutOfMemoryError:
            row["lean"] = "out of memory"
            report["windows"].append(row)
            print(f"{h}x{w}: lean graph out of memory", flush=True)
            del x
            continue
        lean = [t.cpu() for t in lean]          # off the card before the reference is measured
        if (h, w) in IDENTITY_SIZES:
            try:
                ref, row["reference"] = _run(graph.forward_reference, params, x, device)
                ref = [t.cpu() for t in ref]
                row["bit_identical"] = bool(torch.equal(ref[0], lean[0]) and torch.equal(ref[1], lean[1]))
                row["max_abs_diff"] = max(float((ref[0] - lean[0]).abs().max()),
                                          float((ref[1] - lean[1]).abs().max()))
                del ref
            except torch.cuda.OutOfMemoryError:
                row["reference"] = "out of memory"
        del lean, x
        report["windows"].append(row)
        ident = row.get("bit_identical", "-")
        print(f"{h}x{w}: identical={ident} diff={row.get('max_abs_diff', '-')} "
              f"lean peak {row['lean']['peak_allocated_gib']} GiB ({row['lean']['bytes_per_px']} B/px, "
              f"{row['lean']['seconds']} s)"
              + (f" | reference peak {row['reference']['peak_allocated_gib']} GiB, "
                 f"{row['reference']['seconds']} s" if isinstance(row.get("reference"), dict) else ""),
              flush=True)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"report: {args.json}")


if __name__ == "__main__":
    main()
