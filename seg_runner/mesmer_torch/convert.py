"""Keras Mesmer -> the `.pt` layout `weights` reads. Needs TensorFlow, so it
runs only in the converter (`scripts/convert_mesmer_to_torch.py`) and in the
tests that compare against TF -- never in an engine process."""
from . import graph
from .weights import FORMAT


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
