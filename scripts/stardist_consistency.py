"""The StarDist torch port's consistency check (docs/stardist_torch_plan.md §8).

What it measures
----------------
`--layer 1` -- the network's two heads (`prob`, `dist`): TensorFlow's values
against the PyTorch port's, on the same inputs (two synthetic arrays, a dense
DAPI patch, and a 301x257 crop padded the way `stardist` pads before the
network).

`--layer 2` -- the FINAL labels of the real `predict_instances` call (the
product path, including stardist's own padding and cropping): six 512x512 DAPI
windows (three dense, one sparse, one at a tissue edge, one almost empty), a
301x257 rectangle, and one Step2-sized read window. Reported per window:
cell counts, pairs matched at IoU >= 0.5 (one to one, by IoU, both
directions), the mean IoU of the matched pairs, the foreground agreement and
the time each side took.

How it runs
-----------
TensorFlow and PyTorch never share a process: one child computes the reference
(CUDA hidden by default), one child per device computes the torch side, and
this process only builds inputs and compares. That is also how the question
"which device does TF actually use" gets answered -- the child reports it.

Usage (a normal terminal, the fusion environment):
    python scripts/stardist_consistency.py                     # layer 1
    python scripts/stardist_consistency.py --layer 2           # final labels
    python scripts/stardist_consistency.py --layer 2 --devices cpu,cuda
    python scripts/stardist_consistency.py --json /tmp/layer1.json
    python scripts/stardist_consistency.py --inject concat
    python scripts/stardist_consistency.py --inject upsample-mode
    python scripts/stardist_consistency.py --inject upsample-factor   # expected to FAIL loudly

The numbers are a DIAGNOSTIC (plan §8): this script asserts no threshold and
decides nothing. `--inject` breaks the torch graph on purpose, to show that
the comparison notices; `upsample-factor` is expected to fail with a shape
error rather than produce numbers.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import numpy as np

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

SCRIPT = os.path.abspath(__file__)
DEFAULT_OME = "/sda1/fusionflux/cropped_region.ome.tif"
STARDIST_MODEL = "stardist_2D_versatile_fluo"
INJECTIONS = ("none", "concat", "upsample-mode", "upsample-factor")
PATCH = 512
#: Step2's own defaults (ui/step2_page.py: Rows 3, Cols 4, Overlap 200) and its
#: 60 MP cap on one tile's read area; 3x4 stays under the cap for this slide.
STEP2_GRID = (3, 4, 200)


# ── inputs ──────────────────────────────────────────────────────────────────
def _normalize(image):
    """csbdeep's `normalize(img, 1, 99.8)` over the whole patch, in numpy:
    the product's preprocessing, kept out of the child processes."""
    x = np.asarray(image, dtype=np.float32)
    lo, hi = np.percentile(x, (1, 99.8))
    return ((x - lo) / (hi - lo + 1e-20)).astype(np.float32)


def _dapi_channel(ome, level=0):
    """Channel 0 (DAPI) of the reference slide at `level` of its pyramid."""
    import tifffile
    with tifffile.TiffFile(ome) as f:
        pages = f.series[0].levels[level].pages
        return np.asarray(pages[0].asarray())


def _pyramid_levels(ome):
    import tifffile
    with tifffile.TiffFile(ome) as f:
        return f.series[0].levels


def _padded_rectangle(unit):
    height, width = 301, 257
    pad_h = (-height) % unit
    pad_w = (-width) % unit
    return height, width, pad_h, pad_w


def _dense_window(small, big, size=256):
    """Top-left corner of the densest `size`x`size` window of `big`, found on
    the small pyramid level so the big one is not scanned."""
    step_y = max(1, round(size * small.shape[0] / big.shape[0]))
    step_x = max(1, round(size * small.shape[1] / big.shape[1]))
    rows, cols = small.shape[0] // step_y, small.shape[1] // step_x
    means = small[:rows * step_y, :cols * step_x].reshape(rows, step_y, cols, step_x).mean(axis=(1, 3))
    by, bx = np.unravel_index(int(np.argmax(means)), means.shape)
    y = int(by * step_y * big.shape[0] / small.shape[0])
    x = int(bx * step_x * big.shape[1] / small.shape[1])
    return min(y, big.shape[0] - size), min(x, big.shape[1] - size)


def build_cases_layer1(ome, seed=0):
    """[(name, NHWC float32 array)]: the inputs both sides see."""
    rng = np.random.default_rng(seed)
    cases = [("random 256x256", rng.random((1, 256, 256, 1), dtype=np.float32)),
             ("random 2x256x256", rng.random((2, 256, 256, 1), dtype=np.float32))]
    if not ome or not os.path.isfile(ome):
        return cases, f"no reference slide at {ome}: the real-DAPI cases are skipped"
    from seg_runner.stardist_torch import graph
    big = _dapi_channel(ome, 0)
    small = _dapi_channel(ome, len(_pyramid_levels(ome)) - 1)
    y, x = _dense_window(small, big, 256)
    cases.append(("dapi dense 256x256", _normalize(big[y:y + 256, x:x + 256])[None, ..., None]))
    height, width, pad_h, pad_w = _padded_rectangle(graph.PAD_UNIT)
    y2 = min(y, big.shape[0] - height)
    x2 = min(x, big.shape[1] - width)
    crop = _normalize(big[y2:y2 + height, x2:x2 + width])[None, ..., None]
    cases.append((f"dapi {height}x{width} padded -> {height + pad_h}x{width + pad_w}",
                  np.pad(crop, ((0, 0), (0, pad_h), (0, pad_w), (0, 0)), mode="reflect")))
    return cases, ""


def _window_corner(cell, block, big, small, size):
    """Level-0 top-left of preview block `cell`, clamped to fit `size`."""
    y = int(cell[0] * block * big.shape[0] / small.shape[0])
    x = int(cell[1] * block * big.shape[1] / small.shape[1])
    return min(max(y, 0), big.shape[0] - size), min(max(x, 0), big.shape[1] - size)


def _pick_blocks(small, block, wanted=3):
    """Dense / sparse / edge / empty blocks of a preview level."""
    rows, cols = small.shape[0] // block, small.shape[1] // block
    views = small[:rows * block, :cols * block].reshape(rows, block, cols, block)
    means, stds = views.mean(axis=(1, 3)), views.std(axis=(1, 3))
    order = np.argsort(-means.ravel())
    chosen = []
    for flat in order:                       # dense: spread them apart
        row, col = np.unravel_index(int(flat), means.shape)
        if all(max(abs(row - r), abs(col - c)) >= 2 for r, c in chosen):
            chosen.append((int(row), int(col)))
        if len(chosen) == wanted:
            break
    blank = np.unravel_index(int(np.argmin(means)), means.shape)
    positive = means[means > 0.05 * means.max()]
    sparse_target = float(np.percentile(positive, 20)) if positive.size else 0.0
    sparse = np.unravel_index(int(np.argmin(np.abs(means - sparse_target))), means.shape)
    edges = np.where(means >= np.median(means), stds, -1)
    edge = np.unravel_index(int(np.argmax(edges)), means.shape)
    return {
        "dense": chosen,
        "sparse": (int(sparse[0]), int(sparse[1])),
        "edge": (int(edge[0]), int(edge[1])),
        "blank": (int(blank[0]), int(blank[1])),
    }


def _step2_tile(big, small):
    """The read window of the densest Step2 tile (its defaults: 3x4, 200 px)."""
    from utils.tile_scheduler import TileScheduler
    rows, cols, overlap = STEP2_GRID
    scheduler = TileScheduler(big.shape[0], big.shape[1], rows, cols, overlap)
    fy, fx = big.shape[0] / small.shape[0], big.shape[1] / small.shape[1]
    best, best_mean = scheduler.tiles[0], -1.0
    for tile in scheduler.tiles:
        y0, y1, x0, x1 = tile.own_bbox
        view = small[int(y0 / fy):int(y1 / fy), int(x0 / fx):int(x1 / fx)]
        if view.size and view.mean() > best_mean:
            best, best_mean = tile, float(view.mean())
    return best


def build_cases_layer2(ome, seed=0):
    """[(name, 2-D float32 normalized patch)] for the final-label comparison."""
    big = _dapi_channel(ome, 0)
    small = _dapi_channel(ome, len(_pyramid_levels(ome)) - 1)
    block = max(1, round(PATCH * small.shape[0] / big.shape[0]))
    grid = (small.shape[0] // block, small.shape[1] // block)
    picked = _pick_blocks(small, block)
    cases, where = [], []
    for role in ("dense", "sparse", "edge", "blank"):
        cells = picked[role] if isinstance(picked[role], list) else [picked[role]]
        for index, cell in enumerate(cells):
            y, x = _window_corner(cell, block, big, small, PATCH)
            cases.append((f"dapi {role} {PATCH}x{PATCH}",
                          _normalize(big[y:y + PATCH, x:x + PATCH])))
            where.append((role, index, y, x))
    height, width, _, _ = _padded_rectangle(16)
    y, x = _window_corner(picked["dense"][0], block, big, small, height)
    x = min(x, big.shape[1] - width)
    cases.append((f"dapi {height}x{width} (real padding)", _normalize(big[y:y + height, x:x + width])))
    where.append(("padding", 0, y, x))
    tile = _step2_tile(big, small)
    y0, y1, x0, x1 = tile.read_bbox
    cases.append((f"step2 tile {y1 - y0}x{x1 - x0} (own {tile.own_bbox[1] - tile.own_bbox[0]}x"
                  f"{tile.own_bbox[3] - tile.own_bbox[2]}, tile {tile.row},{tile.col})",
                  _normalize(big[y0:y1, x0:x1])))
    where.append(("step2", tile.index, y0, x0))
    return cases, where


# ── the two sides, in child processes ──────────────────────────────────────
def _model(model_dir):
    from stardist.models import StarDist2D
    folder = os.path.abspath(os.path.expanduser(model_dir))
    return StarDist2D(None, name=os.path.basename(folder), basedir=os.path.dirname(folder))


def reference_child(inp, model_dir, mode="network"):
    import tensorflow as tf
    model = _model(model_dir)
    image = np.load(inp)
    if mode == "labels" and image.size <= 2_000_000:
        # TensorFlow traces a graph per input shape on the first call; without
        # this the small windows would time the trace (seconds), not the work.
        model.predict_instances(image)
    started = time.perf_counter()
    if mode == "labels":
        labels, _ = model.predict_instances(image)
        arrays = {"labels": np.asarray(labels, dtype=np.uint32)}
    else:
        prob, dist = model.keras_model.predict(image, verbose=0)[:2]
        arrays = {"prob": prob, "dist": dist}
    return arrays, {"tensorflow": tf.__version__, "mode": mode,
                    "physical_devices": [d.device_type for d in tf.config.list_physical_devices()],
                    "gpus": [d.name for d in tf.config.list_physical_devices("GPU")],
                    "runtime_s": round(time.perf_counter() - started, 3),
                    "shape": list(image.shape)}


def torch_child(inp, mode, model_dir, weights_path, digest, device, inject):
    import torch
    from seg_runner.stardist_torch import graph, network, numerics, weights
    params = weights.load(weights_path, device, digest)
    image = np.load(inp)
    if mode == "labels":
        from seg_runner import engines
        engines._hide_tensorflow_gpu()          # TF is here for the wrapper only (§6.5)
        model = _model(model_dir)
        model.keras_model = network.TorchNetwork(params, device)
        started = time.perf_counter()           # the model is ready: time the prediction only
        with injected(inject):
            labels, _ = model.predict_instances(image)
        runtime = round(time.perf_counter() - started, 3)
        arrays = {"labels": np.asarray(labels, dtype=np.uint32)}
        repeat = None
    else:
        x = torch.from_numpy(np.ascontiguousarray(image)).permute(0, 3, 1, 2).contiguous().to(device)
        started = time.perf_counter()
        with injected(inject), numerics.numerics(device):
            with torch.inference_mode():
                prob, dist = graph.forward(params["tensors"], x)
                prob2, dist2 = graph.forward(params["tensors"], x)      # repeatability
        repeat = max(float((prob - prob2).abs().max()), float((dist - dist2).abs().max()))
        arrays = {"prob": prob.permute(0, 2, 3, 1).float().cpu().numpy(),
                  "dist": dist.permute(0, 2, 3, 1).float().cpu().numpy()}
    return arrays, {"torch": torch.__version__, "device": device, "mode": mode, "inject": inject,
                    "cuda": torch.cuda.is_available(), "repeat_max_abs": repeat,
                    "runtime_s": runtime if mode == "labels" else round(time.perf_counter() - started, 3),
                    "numerics": dict(numerics.NUMERICS),
                    "content_sha256": params["content_sha256"]}


def injected(name):
    """Break the graph on purpose; the comparison must notice."""
    import contextlib

    import torch
    import torch.nn.functional as F

    @contextlib.contextmanager
    def _none():
        yield

    if name in (None, "none"):
        return _none()
    if name == "concat":
        real = torch.cat

        def cat(tensors, dim=0, **kwargs):
            if isinstance(tensors, (list, tuple)) and len(tensors) == 2 and dim == 1:
                tensors = (tensors[1], tensors[0])
            return real(tensors, dim=dim, **kwargs)

        @contextlib.contextmanager
        def _swap():
            torch.cat = cat
            try:
                yield
            finally:
                torch.cat = real
        return _swap()
    real_interp = F.interpolate

    def interp(x, scale_factor=2, mode="nearest", **kwargs):
        if name == "upsample-mode" and mode == "nearest":
            mode = "bilinear"
            kwargs.setdefault("align_corners", False)
        if name == "upsample-factor" and mode == "nearest":
            scale_factor = int(scale_factor) + 1
        return real_interp(x, scale_factor=scale_factor, mode=mode, **kwargs)

    @contextlib.contextmanager
    def _interp():
        F.interpolate = interp
        try:
            yield
        finally:
            F.interpolate = real_interp
    return _interp()


def _write(path, arrays, meta):
    np.savez(path + ".npz", **arrays)
    with open(path + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)


def _read(path):
    with np.load(path + ".npz") as data:
        arrays = {k: data[k] for k in data.files}
    with open(path + ".json", encoding="utf-8") as f:
        meta = json.load(f)
    return arrays, meta


# ── comparisons ────────────────────────────────────────────────────────────
def compare(reference, produced):
    """`reference` is the TensorFlow side (plan §8). dist's mean error is taken
    per channel and then the largest of the 32 is reported; the relative error
    divides by the reference distance."""
    out = {}
    for name in ("prob", "dist"):
        a = np.asarray(reference[name], dtype=np.float64)
        b = np.asarray(produced[name], dtype=np.float64)
        if a.shape != b.shape:
            return {"error": f"{name}: shape {b.shape} != reference {a.shape}"}
        diff = np.abs(a - b)
        out[f"{name}_max_abs"] = float(diff.max())
        if name == "dist":
            out["dist_mean_abs"] = float(diff.mean(axis=(0, 1, 2)).max())   # per channel, then max
            out["dist_max_rel"] = float((diff / np.maximum(np.abs(a), 1.0)).max())
        else:
            out["prob_mean_abs"] = float(diff.mean())
    return out


def label_metrics(reference, produced):
    """Final labels against the reference, with stardist's own matching (one to
    one, IoU >= 0.5). `n_true` is the reference's cell count."""
    from stardist.matching import matching
    a = np.asarray(reference)
    b = np.asarray(produced)
    if a.shape != b.shape:
        return {"error": f"shape {b.shape} != reference {a.shape}"}
    result = matching(a, b, thresh=0.5)
    foreground_a, foreground_b = a > 0, b > 0
    union = int(np.logical_or(foreground_a, foreground_b).sum())
    inter = int(np.logical_and(foreground_a, foreground_b).sum())
    return {"count_reference": int(result.n_true), "count_produced": int(result.n_pred),
            "matched": int(result.tp), "recall": float(result.recall),
            "precision": float(result.precision),
            "mean_iou": float(result.mean_matched_score) if result.tp else 0.0,
            "foreground_agreement": float(inter / union) if union else 1.0}


def _run_child(mode, inp, out, env, extra):
    cmd = [sys.executable, SCRIPT, "--child", mode, "--input", inp, "--out", out] + extra
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--child", choices=("reference", "torch"))
    ap.add_argument("--layer", type=int, default=1, choices=(1, 2))
    ap.add_argument("--input")
    ap.add_argument("--out")
    ap.add_argument("--model-dir")
    ap.add_argument("--weights")
    ap.add_argument("--digest")
    ap.add_argument("--mode", default="network", choices=("network", "labels"))
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--inject", default="none")
    ap.add_argument("--ome", default=DEFAULT_OME, help="reference slide (read only)")
    ap.add_argument("--devices", default="", help="comma list, e.g. cpu,cuda (default: cpu + cuda if present)")
    ap.add_argument("--json", default="", help="write the rows here")
    ap.add_argument("--tf-gpu", action="store_true", help="let the TF reference see the GPU")
    args = ap.parse_args(argv)

    if args.child == "reference":
        arrays, meta = reference_child(args.input, args.model_dir, args.mode)
        _write(args.out, arrays, meta)
        return 0
    if args.child == "torch":
        arrays, meta = torch_child(args.input, args.mode, args.model_dir, args.weights,
                                   args.digest, args.device, args.inject)
        _write(args.out, arrays, meta)
        return 0

    from seg_runner import engines
    from seg_runner.stardist_torch import graph, weights

    try:
        model_dir = engines.model_path(STARDIST_MODEL)
        torch_root = engines.model_path(engines.STARDIST_TORCH_ENTRY)
    except engines.ModelMissingError as exc:
        print(f"cannot run: {exc}")
        return 1
    torch_weights = os.path.join(torch_root, weights.WEIGHTS_FILE)
    digest = engines.manifest_entry(engines.STARDIST_TORCH_ENTRY).get("content_sha256")
    mode = "labels" if args.layer == 2 else "network"

    if not args.ome or not os.path.isfile(args.ome):
        print(f"cannot run layer {args.layer}: no reference slide at {args.ome}")
        return 1
    if args.layer == 2:
        cases, where = build_cases_layer2(args.ome)
        print(f"layer 2 windows (level-0 top-left): {where}")
    else:
        cases, note = build_cases_layer1(args.ome)
        if note:
            print(note)

    if args.devices:
        devices = [d.strip() for d in args.devices.split(",") if d.strip()]
    else:
        devices = ["cpu"] + (["cuda"] if _has_cuda() else [])
        if args.inject != "none":
            devices = ["cpu"] if "cpu" in devices else devices[:1]

    reference_env = dict(os.environ, KERAS_BACKEND="tensorflow", TF_CPP_MIN_LOG_LEVEL="2")
    if not args.tf_gpu:
        reference_env["CUDA_VISIBLE_DEVICES"] = ""
    torch_env = dict(os.environ, TF_CPP_MIN_LOG_LEVEL="2")

    rows, failures = [], 0
    with tempfile.TemporaryDirectory(prefix="stardist_consistency_") as tmp:
        for index, (name, array) in enumerate(cases):
            inp = os.path.join(tmp, f"case{index}.npy")
            np.save(inp, array)
            ref_out = os.path.join(tmp, f"case{index}.ref")
            done = _run_child("reference", inp, ref_out, reference_env,
                              ["--mode", mode, "--model-dir", model_dir])
            if done.returncode != 0:
                print(f"[{name}] the TF reference failed:\n{done.stderr[-2000:]}")
                failures += 1
                continue
            reference, ref_meta = _read(ref_out)
            if index == 0:
                print(f"reference: tensorflow {ref_meta['tensorflow']}, "
                      f"devices {ref_meta['physical_devices']}, GPUs {ref_meta['gpus'] or 'none'}")
            for device in devices:
                out_base = os.path.join(tmp, f"case{index}.{device}.{args.inject}")
                done = _run_child("torch", inp, out_base, dict(torch_env),
                                  ["--mode", mode, "--model-dir", model_dir,
                                   "--weights", torch_weights, "--digest", digest or "",
                                   "--device", device, "--inject", args.inject])
                if done.returncode != 0:
                    detail = ((done.stderr or done.stdout).strip().splitlines() or ["no output"])[-1]
                    rows.append({"case": name, "device": device, "inject": args.inject,
                                 "error": detail})
                    print(f"{name:46s} torch-{device:5s} FAILED: {detail[:110]}")
                    continue
                produced, meta = _read(out_base)
                if mode == "labels":
                    row = {"case": name, "device": device, "inject": args.inject,
                           "tf_s": ref_meta.get("runtime_s"), "torch_s": meta.get("runtime_s")}
                    row.update(label_metrics(reference["labels"], produced["labels"]))
                    rows.append(row)
                    if "error" in row:
                        print(f"{name:46s} torch-{device:5s} {row['error']}")
                    else:
                        print(f"{name:46s} torch-{device:5s} "
                              f"n {row['count_reference']:5d}/{row['count_produced']:5d}  "
                              f"matched {row['matched']:5d}  recall {row['recall']:.4f}  "
                              f"prec {row['precision']:.4f}  IoU {row['mean_iou']:.4f}  "
                              f"fg {row['foreground_agreement']:.4f}  "
                              f"t {row['tf_s']:.2f}/{row['torch_s']:.2f}s"
                              f"{'  [injected]' if args.inject != 'none' else ''}")
                else:
                    row = {"case": name, "device": device, "inject": args.inject,
                           "repeat_max_abs": meta.get("repeat_max_abs")}
                    row.update(compare(reference, produced))
                    rows.append(row)
                    if "error" in row:
                        print(f"{name:44s} torch-{device:5s} {row['error']}")
                    else:
                        print(f"{name:44s} torch-{device:5s} "
                              f"prob max {row['prob_max_abs']:.3e} mean {row['prob_mean_abs']:.3e}  "
                              f"dist max {row['dist_max_abs']:.3e} mean {row['dist_mean_abs']:.3e} "
                              f"rel {row['dist_max_rel']:.3e}  repeat {row['repeat_max_abs']:.1e}"
                              f"{'  [injected]' if args.inject != 'none' else ''}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"layer": args.layer, "rows": rows,
                       "paths": {"model": model_dir, "weights": torch_weights}},
                      f, ensure_ascii=False, indent=1)
        print(f"written {args.json}")
    if failures:
        return 1
    if args.inject == "none" and any("error" in r for r in rows):
        return 1
    return 0


def _has_cuda():
    import torch
    return bool(torch.cuda.is_available())


if __name__ == "__main__":
    sys.exit(main())
