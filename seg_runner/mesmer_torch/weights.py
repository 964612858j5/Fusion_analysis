"""The converted Mesmer weights: one `.pt` file, loaded strictly.

File layout (written by `scripts/convert_mesmer_to_torch.py`):
    {"format": FORMAT, "tensors": {"<layer>.<field>": tensor},
     "eps": {"<bn layer>": float}, "source": {...}}
Loaded with `torch.load(weights_only=True)`: tensors and plain values only,
no code. Every key `graph.expected_shapes()` names must be there with its
shape, and nothing else -- a partial or foreign file is an error, never a
silently half-initialised network.
"""
from . import graph

FORMAT = "block01-mesmer-torch/1"
WEIGHTS_FILE = "mesmer_multiplex_segmentation.pt"


class WeightsError(RuntimeError):
    """The weights file is not a complete Mesmer conversion."""


def check(data):
    """`data` as read from the file -> (tensors, eps); raises WeightsError."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        found = data.get("format") if isinstance(data, dict) else type(data).__name__
        raise WeightsError(f"not a Mesmer torch weights file (format {found!r}, want {FORMAT!r})")
    tensors, eps = data.get("tensors") or {}, data.get("eps") or {}
    want = graph.expected_shapes()
    missing = sorted(set(want) - set(tensors))
    extra = sorted(set(tensors) - set(want))
    if missing or extra:
        raise WeightsError(f"weights do not match the network: {len(missing)} missing "
                           f"{missing[:3]}, {len(extra)} unexpected {extra[:3]}")
    wrong = [(k, tuple(tensors[k].shape), shape) for k, shape in want.items()
             if tuple(tensors[k].shape) != shape]
    if wrong:
        raise WeightsError(f"weights with the wrong shape: {wrong[:3]}")
    bn = graph.batch_norm_layers()
    if sorted(eps) != bn:
        raise WeightsError(f"batch-norm epsilons do not match the network "
                           f"({len(eps)} given, {len(bn)} layers)")
    return tensors, {k: float(v) for k, v in eps.items()}


def load(path, device="cpu"):
    """The network parameters in `path`, as float32 tensors on `device`."""
    import torch
    data = torch.load(path, map_location="cpu", weights_only=True)
    tensors, eps = check(data)
    return {"tensors": {k: v.to(device=device, dtype=torch.float32).contiguous()
                        for k, v in tensors.items()},
            "eps": eps, "source": dict(data.get("source") or {})}
