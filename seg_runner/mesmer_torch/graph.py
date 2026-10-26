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
# CHANGES: the Mesmer network (deepcell PanopticNet: Location2D, ResNet50
# backbone, P3 of the FPN, four semantic heads), as built by the installed
# deepcell and saved in its MultiplexSegmentation model, re-written as a
# functional PyTorch forward pass with the same layer names. No layer,
# activation or weight is changed.
"""The Mesmer network as one function: `forward(params, x) -> four heads`.

Functional on purpose -- no `nn.Module`, so batch normalisation is always the
inference form and nothing has a training mode to forget. `params` is what
`weights.load` returns: a tensor per `<layer>.<field>` key and each batch-norm
layer's own epsilon.

Layout facts of the saved Keras graph this follows (checked against it):
- the 2 input channels get two coordinate maps (row, column; ij grid divided
  by n - 1), then a 1x1 conv down to 3 channels;
- Keras-applications ResNet50: explicit zero padding 3 before the 7x7/2 conv
  and 1 before the 3x3/2 max pool, the stride on the FIRST 1x1 conv of a
  stage, every conv with a bias, batch-norm epsilon 1.001e-5;
- only P3 of the pyramid: C5r -> bilinear x2 + C4r -> bilinear x2 + C3r ->
  3x3 conv;
- each head: three (3x3 conv, bilinear x2) with no activation between them,
  then 1x1 conv(128) -> batch norm (epsilon 1e-3) -> ReLU -> 1x1 conv; heads
  0 and 2 end in ReLU (inner distance), 1 and 3 in a softmax over 3 classes.
"""

INPUT_SHAPE = (256, 256)
STAGES = ((2, 3, 64, 1), (3, 4, 128, 2), (4, 6, 256, 2), (5, 3, 512, 2))  # stage, blocks, width, stride
HEADS = ((0, 1, "relu"), (1, 3, "softmax"), (2, 1, "relu"), (3, 3, "softmax"))  # head, classes, activation


def _conv(name, k, cin, cout):
    return {f"{name}.weight": (cout, cin, k, k), f"{name}.bias": (cout,)}


def _bn(name, c):
    return {f"{name}.{f}": (c,) for f in ("gamma", "beta", "mean", "var")}


def expected_shapes():
    """Every parameter key of the network and its tensor shape (OIHW convs)."""
    shapes = {}
    shapes.update(_conv("conv_channels", 1, 4, 3))
    shapes.update(_conv("conv1_conv", 7, 3, 64))
    shapes.update(_bn("conv1_bn", 64))
    cin = 64
    for stage, blocks, width, _stride in STAGES:
        for b in range(1, blocks + 1):
            p = f"conv{stage}_block{b}_"
            if b == 1:
                shapes.update(_conv(p + "0_conv", 1, cin, 4 * width))
                shapes.update(_bn(p + "0_bn", 4 * width))
            shapes.update(_conv(p + "1_conv", 1, cin if b == 1 else 4 * width, width))
            shapes.update(_bn(p + "1_bn", width))
            shapes.update(_conv(p + "2_conv", 3, width, width))
            shapes.update(_bn(p + "2_bn", width))
            shapes.update(_conv(p + "3_conv", 1, width, 4 * width))
            shapes.update(_bn(p + "3_bn", 4 * width))
        cin = 4 * width
    for name, c in (("C5_reduced", 2048), ("C4_reduced", 1024), ("C3_reduced", 512)):
        shapes.update(_conv(name, 1, c, 256))
    shapes.update(_conv("P3", 3, 256, 256))
    for i, classes, _act in HEADS:
        shapes.update(_conv(f"conv_0_semantic_upsample_{i}", 3, 256, 64))
        shapes.update(_conv(f"conv_1_semantic_upsample_{i}", 3, 64, 64))
        shapes.update(_conv(f"conv_2_semantic_upsample_{i}", 3, 64, 64))
        shapes.update(_conv(f"conv_0_semantic_{i}", 1, 64, 128))
        shapes.update(_bn(f"batch_normalization_0_semantic_{i}", 128))
        shapes.update(_conv(f"conv_1_semantic_{i}", 1, 128, classes))
    return shapes


def batch_norm_layers():
    return sorted({k.rsplit(".", 1)[0] for k in expected_shapes() if k.endswith(".gamma")})


def forward(params, x):
    """x: float32 tensor N x 2 x 256 x 256 (NCHW). Returns the four heads,
    each N x C x 256 x 256, in the saved model's output order."""
    import torch
    import torch.nn.functional as F

    t, eps = params["tensors"], params["eps"]

    def conv(y, name, stride=1, pad=0):
        return F.conv2d(y, t[name + ".weight"], t[name + ".bias"], stride=stride, padding=pad)

    def bn(y, name):
        return F.batch_norm(y, t[name + ".mean"], t[name + ".var"], t[name + ".gamma"],
                            t[name + ".beta"], False, 0.0, eps[name])

    def up(y):
        return F.interpolate(y, scale_factor=2, mode="bilinear", align_corners=False)

    n, _c, h, w = x.shape
    rows = torch.arange(h, dtype=x.dtype, device=x.device) / (h - 1)
    cols = torch.arange(w, dtype=x.dtype, device=x.device) / (w - 1)
    grid_r, grid_c = torch.meshgrid(rows, cols, indexing="ij")
    y = torch.cat([x, torch.stack([grid_r, grid_c])[None].expand(n, -1, -1, -1)], 1)
    y = conv(y, "conv_channels")
    y = F.relu(bn(conv(F.pad(y, (3, 3, 3, 3)), "conv1_conv", 2), "conv1_bn"))
    y = F.max_pool2d(F.pad(y, (1, 1, 1, 1)), 3, 2)       # after a ReLU: 0 padding == -inf
    feats = {}
    for stage, blocks, _width, stride in STAGES:
        for b in range(1, blocks + 1):
            p = f"conv{stage}_block{b}_"
            s = stride if b == 1 else 1
            short = bn(conv(y, p + "0_conv", s), p + "0_bn") if b == 1 else y
            z = F.relu(bn(conv(y, p + "1_conv", s), p + "1_bn"))
            z = F.relu(bn(conv(z, p + "2_conv", 1, 1), p + "2_bn"))
            y = F.relu(short + bn(conv(z, p + "3_conv"), p + "3_bn"))
        feats[stage] = y
    p4 = conv(feats[4], "C4_reduced") + up(conv(feats[5], "C5_reduced"))
    p3 = conv(conv(feats[3], "C3_reduced") + up(p4), "P3", 1, 1)
    heads = []
    for i, _classes, act in HEADS:
        z = p3
        for j in range(3):
            z = up(conv(z, f"conv_{j}_semantic_upsample_{i}", 1, 1))
        z = F.relu(bn(conv(z, f"conv_0_semantic_{i}"), f"batch_normalization_0_semantic_{i}"))
        z = conv(z, f"conv_1_semantic_{i}")
        heads.append(F.relu(z) if act == "relu" else F.softmax(z, 1))
    return heads
