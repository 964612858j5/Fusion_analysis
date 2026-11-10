# The network structure this wrapper stands in for is StarDist 0.9.2 /
# CSBDeep 0.8.2's (BSD-3-Clause, Uwe Schmidt and Martin Weigert); the code is
# ours. See NOTICE and LICENSE-stardist / LICENSE-csbdeep in this directory.
"""What replaces `StarDist2D.keras_model`: the whole contract the prediction
loop uses.

stardist 0.9.2 touches the network in exactly one place, `predict_direct`
(models/base.py:408-410): `self.keras_model.predict(x[None], verbose=0)`, whose
result is indexed as `(prob, dist)`. `input_shape`, `output_shape` and
`outputs` belong to training and `export_TF`, which the product does not run.

`TorchNetwork` takes NHWC float32 (already normalized and padded by stardist),
runs `graph.forward` on the device `torch_device()` picked, and hands the two
heads back as NHWC float32. The spatial size halves because of the grid.
"""
import numpy as np
import torch

from . import graph, numerics


class TorchNetwork:
    """Stands in for the Keras model; `predict` is the contract (plan §2.2)."""

    def __init__(self, params, device):
        self.params = params["tensors"]
        self.device = device
        self.content_sha256 = params.get("content_sha256") or ""
        self.source = dict(params.get("source") or {})
        #: the shapes the Keras model reported, for anything that asks; the
        #: prediction path does not read them.
        self.input_shape = (None, None, None, graph.N_CHANNEL_IN)
        self.output_shape = [(None, None, None, 1), (None, None, None, graph.N_RAYS)]

    def predict(self, x, verbose=0, **kwargs):
        """`x`: (N, H, W, 1) NHWC float32 -> [prob (N, H/2, W/2, 1), dist
        (N, H/2, W/2, 32)] float32. stardist passes `verbose=0`; it means
        nothing here and is accepted like the Keras model did."""
        array = np.asarray(x)
        if array.ndim != 4 or array.shape[-1] != graph.N_CHANNEL_IN:
            raise ValueError(f"expected (N, H, W, {graph.N_CHANNEL_IN}), got {array.shape}")
        tensor = torch.from_numpy(np.ascontiguousarray(array, dtype=np.float32))
        tensor = tensor.permute(0, 3, 1, 2).to(self.device)
        with numerics.numerics(self.device):
            with torch.inference_mode():
                prob, dist = graph.forward(self.params, tensor)
        return [_nhwc(prob), _nhwc(dist)]


def _nhwc(tensor):
    return tensor.permute(0, 2, 3, 1).float().cpu().numpy()
