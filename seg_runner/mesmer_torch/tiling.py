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
# CHANGES: Application._tile_input and Application._untile_output as two
# functions; a padded window that is not 256 x 256 is refused with our own error.
"""The model's fixed 256 x 256 window over an image of any size, as Mesmer
does it: an image smaller than the window on either axis is reflect-padded
symmetrically (an odd difference puts the extra pixel on the high side);
anything else is cut by deepcell_toolbox `tile_image` (stride 0.75) and put
back by its `untile_image`."""
import numpy as np

MODEL_SHAPE = (256, 256)
STRIDE_RATIO = 0.75


def tile(image, pad_mode="constant"):
    """(tiles N x 256 x 256 x C, info) for `image` (B, H, W, C)."""
    from deepcell_toolbox.utils import tile_image
    if image.ndim != 4:
        raise ValueError(f"tiling needs a 4D image, got shape {image.shape}")
    x_diff = image.shape[1] - MODEL_SHAPE[0]
    y_diff = image.shape[2] - MODEL_SHAPE[1]
    if x_diff < 0 or y_diff < 0:
        # As the original: BOTH axes padded by |difference|, so an image
        # smaller on one axis but larger on the other (300 x 200) becomes
        # 344 x 256 -- which the fixed-size Keras model refused. Refused here
        # too, rather than silently run on a window the model never took.
        x_diff, y_diff = abs(x_diff), abs(y_diff)
        x_pad = (x_diff // 2, x_diff // 2 + 1) if x_diff % 2 else (x_diff // 2, x_diff // 2)
        y_pad = (y_diff // 2, y_diff // 2 + 1) if y_diff % 2 else (y_diff // 2, y_diff // 2)
        tiles = np.pad(image, [(0, 0), x_pad, y_pad, (0, 0)], "reflect")
        if tiles.shape[1:3] != MODEL_SHAPE:
            raise ValueError(f"Mesmer cannot take a {image.shape[1]} x {image.shape[2]} image: "
                             f"padded it is {tiles.shape[1]} x {tiles.shape[2]}, the model "
                             f"takes {MODEL_SHAPE[0]} x {MODEL_SHAPE[1]}")
        return tiles, {"padding": True, "x_pad": x_pad, "y_pad": y_pad}
    return tile_image(image, model_input_shape=MODEL_SHAPE, stride_ratio=STRIDE_RATIO,
                      pad_mode=pad_mode)


def untile(output_tiles, info):
    """Each model output (N x 256 x 256 x C) back to the image's size."""
    from deepcell_toolbox.utils import untile_image
    if info.get("padding", False):
        (xl, xh), (yl, yh) = info["x_pad"], info["y_pad"]
        xh = -xh if xh != 0 else None
        yh = -yh if yh != 0 else None
        return [o[:, xl:xh, yl:yh, :] for o in output_tiles]
    return [untile_image(o, info, model_input_shape=MODEL_SHAPE) for o in output_tiles]
