# The weights handled here are StarDist 0.9.2's own Keras weights converted
# to torch (StarDist and CSBDeep are BSD-3-Clause, Uwe Schmidt and Martin
# Weigert); the code is ours. See NOTICE and LICENSE-stardist /
# LICENSE-csbdeep in this directory.
"""The converted StarDist weights: one `.pt` file, loaded strictly.

File layout (written by `convert`):
    {"format": FORMAT, "tensors": {"<layer>.<field>": tensor},
     "source": {...}}
Loaded with `torch.load(weights_only=True)`: tensors and plain values only,
no code. Every key `graph.expected_shapes()` names must be there with its
shape, and nothing else -- a partial or foreign file is an error, never a
silently half-initialised network.

The file is identified by its CONTENT (`content_digest`), not its bytes: a
conversion on another machine stores other paths in `source` and pickles
differently, but the tensors are the same (docs/stardist_torch_plan.md §4.4).
"""
import hashlib

from . import graph

FORMAT = "block01-stardist-torch/1"
WEIGHTS_FILE = "stardist_2D_versatile_fluo.pt"


class WeightsError(RuntimeError):
    """The weights file is not a complete StarDist conversion."""


def check(data):
    """`data` as read from the file -> the tensors; raises WeightsError."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        found = data.get("format") if isinstance(data, dict) else type(data).__name__
        raise WeightsError(f"not a StarDist torch weights file (format {found!r}, want {FORMAT!r})")
    tensors = data.get("tensors") or {}
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
    return tensors


def content_digest(data):
    """sha256 of what the network computes with: FORMAT, then every tensor in
    name order (name, dtype, shape, byte length, raw bytes). `source` (paths,
    versions) is left out. `data` is the file as read -- before any dtype or
    device change."""
    tensors = check(data)
    h = hashlib.sha256()
    h.update(FORMAT.encode() + b"\0")
    for name in sorted(tensors):
        t = tensors[name].detach().cpu().contiguous()
        raw = t.numpy().tobytes()
        h.update(b"\0".join([name.encode(), str(t.dtype).encode(),
                             ",".join(map(str, t.shape)).encode(), str(len(raw)).encode(), raw]) + b"\0")
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
        raise WeightsError("no expected content digest for the StarDist torch weights "
                           "(the manifest entry has no content_sha256)")
    data = read(path)
    tensors = check(data)
    found = content_digest(data)
    if found != expected_digest:
        raise WeightsError(f"{path} holds other weights than the manifest names "
                           f"(content {found[:12]}..., want {expected_digest[:12]}...); "
                           "run scripts/convert_stardist_to_torch.py --force to remake it")
    return {"tensors": {k: v.to(device=device, dtype=torch.float32).contiguous()
                        for k, v in tensors.items()},
            "source": dict(data.get("source") or {}), "content_sha256": found}
