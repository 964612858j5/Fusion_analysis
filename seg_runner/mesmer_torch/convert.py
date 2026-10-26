"""Keras Mesmer -> the `.pt` layout `weights` reads. Needs TensorFlow, so it
runs only in its own CPU-only child process (`prepare` starts it:
`python -m <package>.convert --source DIR --out FILE`) and in the tests that
compare against TF -- never in an engine process.

Exit codes of the child: 0 written and checked, NO_TENSORFLOW, HEAD_CHECK
(the four heads differ from Keras by more than TOLERANCE); anything else is
an unexpected error with its traceback on stderr."""
import argparse
import hashlib
import os
import pathlib
import sys

from . import graph
from .weights import FORMAT, check

TOLERANCE = 1e-4
NO_TENSORFLOW = 3
HEAD_CHECK = 4


def from_keras(model):
    """{"tensors", "eps"} of a loaded Keras Mesmer model: conv kernels HWIO ->
    OIHW, batch norms as gamma/beta/moving mean/moving variance with each
    layer's own epsilon. Only layers the network uses are taken."""
    import numpy as np
    import torch
    want = graph.expected_shapes()
    layers = {k.rsplit(".", 1)[0] for k in want}
    tensors, eps = {}, {}
    for layer in model.layers:
        if layer.name not in layers:
            continue
        kind = type(layer).__name__
        w = layer.get_weights()
        if kind == "Conv2D":
            tensors[f"{layer.name}.weight"] = torch.from_numpy(
                np.ascontiguousarray(np.transpose(w[0], (3, 2, 0, 1))))
            tensors[f"{layer.name}.bias"] = torch.from_numpy(np.asarray(w[1]))
        elif kind == "BatchNormalization":
            cfg = layer.get_config()
            if not cfg.get("scale", True) or not cfg.get("center", True):
                raise ValueError(f"{layer.name}: batch norm without scale/center is not supported")
            for field, arr in zip(("gamma", "beta", "mean", "var"), w):
                tensors[f"{layer.name}.{field}"] = torch.from_numpy(np.asarray(arr))
            eps[layer.name] = float(cfg["epsilon"])
        else:
            raise ValueError(f"unexpected layer {layer.name} of type {kind}")
    return {"format": FORMAT, "tensors": tensors, "eps": eps}


def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def head_diffs(keras_model, data):
    """max |Keras - torch| of each of the four heads on a fixed random input."""
    import numpy as np
    import torch
    tensors, eps = check(data)
    params = {"tensors": {k: v.float() for k, v in tensors.items()}, "eps": eps}
    x = np.random.default_rng(0).random((2, *graph.INPUT_SHAPE, 2)).astype(np.float32)
    want = keras_model.predict(x, batch_size=1, verbose=0)
    with torch.inference_mode():
        got = graph.forward(params, torch.from_numpy(x).permute(0, 3, 1, 2).contiguous())
    return [float(np.abs(a - b.permute(0, 2, 3, 1).numpy()).max()) for a, b in zip(want, got)]


def convert_dir(src, out):
    """Convert the Keras model directory `src` into the file `out`; returns
    the head differences. Raises if they exceed TOLERANCE (nothing written)."""
    import tensorflow as tf
    import torch
    from importlib import metadata
    src = pathlib.Path(src)
    keras_model = tf.keras.models.load_model(str(src), compile=False)
    data = from_keras(keras_model)
    diffs = head_diffs(keras_model, data)
    if max(diffs) > TOLERANCE:
        raise HeadCheckError(f"head differences {diffs} exceed {TOLERANCE}")
    names = sorted(str(p.relative_to(src)) for p in src.rglob("*") if p.is_file())
    data["source"] = {"model_key": "mesmer_multiplex_segmentation", "root": str(src),
                      "files": {n: _sha256(src / n) for n in names},
                      "deepcell": metadata.version("deepcell"), "tensorflow": tf.__version__,
                      "torch": str(torch.__version__), "head_diffs": diffs, "tolerance": TOLERANCE}
    torch.save(data, out)
    return diffs


class HeadCheckError(RuntimeError):
    """The converted network does not reproduce the Keras heads."""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
    try:
        import tensorflow  # noqa: F401
    except ImportError as exc:
        print(f"[Mesmer] TensorFlow is not installed here: {exc}", file=sys.stderr)
        return NO_TENSORFLOW
    try:
        diffs = convert_dir(args.source, args.out)
    except HeadCheckError as exc:
        print(f"[Mesmer] conversion check failed: {exc}", file=sys.stderr)
        return HEAD_CHECK
    print(f"[Mesmer] converted; head differences {', '.join(f'{d:.2e}' for d in diffs)}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
