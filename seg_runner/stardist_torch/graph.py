"""The 2D StarDist network (2D_versatile_fluo) as a PyTorch forward pass.

`csbdeep`'s U-Net for this model is: two 3x3 convolutions, a 2x2 max pool
(the `grid`), a U-Net with three down levels (32, 64, 128 filters), a middle
with 256 -> 128, three up levels with a nearest-neighbour upsample and a skip
concatenation, one 3x3 convolution with 128 filters (`features`), and the two
1x1 heads: `prob` (sigmoid) and `dist` (linear, `n_rays` = 32).

The graph is written from `config.json` and the published architecture
(`csbdeep/internals/blocks.py`, `stardist/models/model2d.py:_build`) -- the
code below is ours, the structure is theirs; see NOTICE. There is no
BatchNorm and no dropout, so a pass needs no module state and no train/eval
mode: a plain function over the parameter tensors is the whole network.

Contract: input `(N, 1, H, W)` float32, output
`[prob (N, 1, H/2, W/2), dist (N, 32, H/2, W/2)]`. The spatial size halves
because of the grid pool; the caller (`stardist`'s sparse prediction) expects
exactly that.
"""
# The network structure read here is StarDist 0.9.2 / CSBDeep 0.8.2's
# (BSD-3-Clause, Uwe Schmidt and Martin Weigert); the code below is ours.
# See NOTICE and LICENSE-stardist / LICENSE-csbdeep in this directory.
import torch
import torch.nn.functional as F

#: `config.json` values this port reproduces (2D_versatile_fluo). `convert`
#: refuses any other config: a different architecture is a different job.
GRID = (2, 2)            # `grid`: the pool before the U-Net, and the output / input ratio
POOL = (2, 2)            # `unet_pool`: the max pool and the upsample of each U-Net level
N_RAYS = 32
N_CHANNEL_IN = 1
N_DEPTH = 3
N_CONV_PER_DEPTH = 2
N_FILTER_BASE = 32
CONV_AFTER_UNET = 128
KERNEL_SIZE = (3, 3)
BATCH_NORM = False
DROPOUT = 0.0
#: the divisor `stardist` pads the input to before the network
#: (`_axes_div_by` = unet_pool ** unet_n_depth * grid).
PAD_UNIT = POOL[0] ** N_DEPTH * GRID[0]

#: `config.json` keys a conversion must see exactly (the rest is policy).
CONFIG = {
    "backbone": "unet",
    "n_dim": 2,
    "axes": "YXC",
    "n_channel_in": N_CHANNEL_IN,
    "n_channel_out": 1 + N_RAYS,
    "n_rays": N_RAYS,
    "grid": list(GRID),
    "unet_pool": list(POOL),
    "unet_n_depth": N_DEPTH,
    "unet_n_filter_base": N_FILTER_BASE,
    "unet_n_conv_per_depth": N_CONV_PER_DEPTH,
    "unet_kernel_size": list(KERNEL_SIZE),
    "unet_activation": "relu",           # the forward below hard-codes ReLU
    "unet_last_activation": "relu",      # ... and so does the last up level
    "unet_batch_norm": BATCH_NORM,
    "unet_dropout": DROPOUT,
    "net_conv_after_unet": CONV_AFTER_UNET,
}


def layer_specs():
    """`(layer, channels in, channels out, kernel)` in forward order.

    Mirrors `stardist/models/model2d.py:_build` and
    `csbdeep/internals/blocks.py:unet_block`; the two heads are 1x1."""
    specs = [("conv2d_1", N_CHANNEL_IN, N_FILTER_BASE, KERNEL_SIZE),
             ("conv2d_2", N_FILTER_BASE, N_FILTER_BASE, KERNEL_SIZE)]
    prev = N_FILTER_BASE
    for n in range(N_DEPTH):
        out = N_FILTER_BASE * 2 ** n
        for i in range(N_CONV_PER_DEPTH):
            specs.append((f"down_level_{n}_no_{i}", prev, out, KERNEL_SIZE))
            prev = out
    for i in range(N_CONV_PER_DEPTH - 1):
        out = N_FILTER_BASE * 2 ** N_DEPTH
        specs.append((f"middle_{i}", prev, out, KERNEL_SIZE))
        prev = out
    out = N_FILTER_BASE * 2 ** max(0, N_DEPTH - 1)
    specs.append((f"middle_{N_CONV_PER_DEPTH}", prev, out, KERNEL_SIZE))
    prev = out
    for n in reversed(range(N_DEPTH)):
        entered = prev + N_FILTER_BASE * 2 ** n          # after the concatenation
        out = N_FILTER_BASE * 2 ** n
        for i in range(N_CONV_PER_DEPTH - 1):
            specs.append((f"up_level_{n}_no_{i}", entered, out, KERNEL_SIZE))
            entered = out
        out = N_FILTER_BASE * 2 ** max(0, n - 1)
        specs.append((f"up_level_{n}_no_{N_CONV_PER_DEPTH}", entered, out, KERNEL_SIZE))
        prev = out
    specs.append(("features", prev, CONV_AFTER_UNET, KERNEL_SIZE))
    specs.append(("prob", CONV_AFTER_UNET, 1, (1, 1)))
    specs.append(("dist", CONV_AFTER_UNET, N_RAYS, (1, 1)))
    return specs


def expected_shapes():
    """`{"<layer>.weight": (out, in, kh, kw), "<layer>.bias": (out,)}`."""
    shapes = {}
    for name, cin, cout, kernel in layer_specs():
        shapes[f"{name}.weight"] = (cout, cin, kernel[0], kernel[1])
        shapes[f"{name}.bias"] = (cout,)
    return shapes


def layer_names():
    return [name for name, _, _, _ in layer_specs()]


def forward(params, x):
    """The network: `x` is `(N, 1, H, W)` float32; returns `[prob, dist]`.

    Every parameter is looked up by name, so a missing or misshaped tensor
    fails here instead of silently running half a network."""
    if x.ndim != 4 or x.shape[1] != N_CHANNEL_IN:
        raise ValueError(f"expected (N, {N_CHANNEL_IN}, H, W), got {tuple(x.shape)}")
    if x.dtype != torch.float32:
        x = x.to(torch.float32)

    def conv(name, h, kernel):
        w = params[f"{name}.weight"]
        b = params[f"{name}.bias"]
        if kernel[0] == 3:
            h = F.pad(h, (1, 1, 1, 1))
        return F.conv2d(h, w, b)

    def block(name, h, kernel=KERNEL_SIZE):
        return F.relu(conv(name, h, kernel))

    h = block("conv2d_1", x)
    h = block("conv2d_2", h)
    h = F.max_pool2d(h, GRID)

    skips = []
    for n in range(N_DEPTH):
        for i in range(N_CONV_PER_DEPTH):
            h = block(f"down_level_{n}_no_{i}", h)
        skips.append(h)
        h = F.max_pool2d(h, POOL)

    for i in range(N_CONV_PER_DEPTH - 1):
        h = block(f"middle_{i}", h)
    h = block(f"middle_{N_CONV_PER_DEPTH}", h)

    for n in reversed(range(N_DEPTH)):
        h = F.interpolate(h, scale_factor=POOL[0], mode="nearest")
        h = torch.cat([h, skips[n]], dim=1)
        for i in range(N_CONV_PER_DEPTH - 1):
            h = block(f"up_level_{n}_no_{i}", h)
        h = block(f"up_level_{n}_no_{N_CONV_PER_DEPTH}", h)

    h = block("features", h)
    prob = torch.sigmoid(conv("prob", h, (1, 1)))
    dist = conv("dist", h, (1, 1))
    return [prob, dist]
