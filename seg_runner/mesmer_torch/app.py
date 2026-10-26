# Copyright 2016-2024 The Van Valen Lab at the California Institute of
# Technology (Caltech), with support from the Paul Allen Family Foundation,
# Google, & National Institutes of Health (NIH) under Grant U24CA224309-01.
# All rights reserved.
#
# Licensed under a modified Apache License, Version 2.0 -- see LICENSE-deepcell
# in this directory. The Work provided may be used for non-commercial academic
# purposes only. For any other use of the Work, including commercial use,
# please contact: vanvalenlab@gmail.com. See also NOTICE.
#
# Adapted from deepcell-tf 0.12.10 by Block01.
# CHANGES: the prediction flow of Application._predict_segmentation,
# _run_model and _batch_predict and of Mesmer.predict, with the Keras model
# replaced by `graph.forward` on PyTorch; the network runs ONCE per tile and
# every requested compartment is post-processed and resized from that one
# output (the original ran the whole flow once per compartment).
"""Mesmer's predict, without TensorFlow.

Order, as the original: input check -> to 0.5 um/px -> preprocess -> tile
-> network (one tile at a time) -> untile -> format -> per compartment:
deep_watershed, then back to the input's size.
"""
import contextlib

import numpy as np

from . import graph, postprocess, preprocess, resize, tiling

COMPARTMENTS = tuple(postprocess.DEFAULTS)
#: What the network runs under (recorded in provenance by the engine).
NUMERICS = {"dtype": "float32", "cudnn_deterministic": True, "cudnn_benchmark": False,
            "tf32": False, "batch_size": 1}


@contextlib.contextmanager
def _numerics():
    """The settings above for the network pass only, restored afterwards:
    they are process-wide switches, and another engine or test in the same
    process must not inherit them (torch.use_deterministic_algorithms is
    not used for that reason -- it also changes how cuBLAS may run)."""
    import torch
    b = torch.backends
    saved = (b.cudnn.deterministic, b.cudnn.benchmark, b.cudnn.allow_tf32, b.cuda.matmul.allow_tf32)
    b.cudnn.deterministic, b.cudnn.benchmark = True, False
    b.cudnn.allow_tf32 = b.cuda.matmul.allow_tf32 = False
    try:
        yield
    finally:
        (b.cudnn.deterministic, b.cudnn.benchmark, b.cudnn.allow_tf32, b.cuda.matmul.allow_tf32) = saved


class MesmerTorch:
    def __init__(self, params, device="cpu"):
        self.params = params
        self.device = device

    def _network(self, tiles):
        """The four heads for every tile, batch 1 (the product's batch size;
        a batched pass is not assumed equal to one tile at a time). Outputs
        are stored in the tiles' dtype, as `_batch_predict` did."""
        import torch
        heads = None
        with torch.inference_mode(), _numerics():
            for i in range(tiles.shape[0]):
                x = torch.from_numpy(np.ascontiguousarray(tiles[i:i + 1], dtype=np.float32))
                out = graph.forward(self.params, x.permute(0, 3, 1, 2).contiguous().to(self.device))
                out = [o.permute(0, 2, 3, 1).cpu().numpy() for o in out]
                if heads is None:
                    heads = [np.zeros((tiles.shape[0],) + o.shape[1:], dtype=tiles.dtype) for o in out]
                for j, o in enumerate(out):
                    heads[j][i:i + 1] = o
        return heads

    def predict(self, image, image_mpp=None, compartments=("whole-cell",), kwargs=None,
                pad_mode="constant"):
        """{compartment: label image (B, H, W, 1)} for `image` (B, H, W, 2).

        `kwargs` maps a compartment to the post-processing arguments that
        replace its defaults (a compartment not named keeps them all)."""
        if image.ndim != 4:
            raise ValueError(f"Input data must have 4 dimensions. Input data only has {image.ndim} dimensions")
        if image.shape[-1] != 2:
            raise ValueError(f"Input data must have 2 channels. Input data only has {image.shape[-1]} channels")
        unknown = [c for c in compartments if c not in COMPARTMENTS]
        if unknown or not compartments:
            raise ValueError(f"compartments must be among {COMPARTMENTS}, got {compartments}")
        kwargs = dict(kwargs or {})
        resized = resize.to_model_resolution(image, image_mpp)
        tiles, info = tiling.tile(preprocess.mesmer_preprocess(resized), pad_mode=pad_mode)
        formatted = postprocess.format_outputs(tiling.untile(self._network(tiles), info))
        return {c: resize.labels_to_input_size(postprocess.segment(formatted, c, kwargs.get(c)), image.shape)
                for c in compartments}
