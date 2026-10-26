"""The converted Mesmer weights: one `.pt` file, loaded strictly.

File layout (written by `convert`, published by `prepare`):
    {"format": FORMAT, "tensors": {"<layer>.<field>": tensor},
     "eps": {"<bn layer>": float}, "source": {...}}
Loaded with `torch.load(weights_only=True)`: tensors and plain values only,
no code. Every key `graph.expected_shapes()` names must be there with its
shape, and nothing else -- a partial or foreign file is an error, never a
silently half-initialised network.

The file is identified by its CONTENT (`content_digest`), not its bytes: a
conversion on another machine stores other paths in `source` and pickles
differently, but the tensors and epsilons are the same, bit for bit
(docs/mesmer_torch_plan.md §10).
"""
import hashlib

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


def content_digest(data):
    """sha256 of what the network computes with: FORMAT, then every tensor in
    name order (name, dtype, shape, byte length, raw bytes), then every
    epsilon in name order. `source` (paths, versions) is left out. `data` is
    the file as read -- before any dtype or device change."""
    tensors, eps = check(data)
    h = hashlib.sha256()
    h.update(FORMAT.encode() + b"\0")
    for name in sorted(tensors):
        t = tensors[name].detach().cpu().contiguous()
        raw = t.numpy().tobytes()
        h.update(b"\0".join([name.encode(), str(t.dtype).encode(),
                             ",".join(map(str, t.shape)).encode(), str(len(raw)).encode(), raw]) + b"\0")
    for name in sorted(eps):
        h.update(f"{name}\0{eps[name]!r}\0".encode())
    return h.hexdigest()


def read(path):
    """The file as stored (CPU, original dtypes); no code is unpickled."""
    import torch
    return torch.load(path, map_location="cpu", weights_only=True)


def load(path, device, expected_digest):
    """The network parameters in `path`, as float32 tensors on `device`.
    Their content digest must equal `expected_digest` (the manifest's); a
    mismatch refuses the file and leaves it where it is."""
    import torch
    if not expected_digest:
        raise WeightsError("no expected content digest for the Mesmer weights "
                           "(the manifest entry has no content_sha256)")
    data = read(path)
    tensors, eps = check(data)
    found = content_digest(data)
    if found != expected_digest:
        raise WeightsError(f"{path} holds other weights than the manifest names "
                           f"(content {found[:12]}..., want {expected_digest[:12]}...); "
                           "move it away and start again to regenerate it")
    return {"tensors": {k: v.to(device=device, dtype=torch.float32).contiguous()
                        for k, v in tensors.items()},
            "eps": eps, "source": dict(data.get("source") or {}), "content_sha256": found}
