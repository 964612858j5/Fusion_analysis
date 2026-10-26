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
# CHANGES: Application._resize_input and Application._resize_output as two
# functions on plain arrays (one label image per call); the resampling itself
# is still deepcell_toolbox.utils.resize.
"""Mesmer's two resolution changes, through deepcell_toolbox's own `resize`:
the input to the model's 0.5 um/px before anything else, and each label
image back to the input's size at the very end."""
import numpy as np

MODEL_MPP = 0.5


def to_model_resolution(image, image_mpp):
    """`image` (B, H, W, C) resampled to MODEL_MPP; unchanged when `image_mpp`
    is None or already MODEL_MPP. New size int(size * image_mpp / 0.5); the
    toolbox resamples multi-channel data with skimage (linear, anti-aliased,
    constant mode, range and dtype kept)."""
    if image_mpp in {None, MODEL_MPP}:
        return image
    from deepcell_toolbox.utils import resize
    scale = image_mpp / MODEL_MPP
    shape = (int(image.shape[1] * scale), int(image.shape[2] * scale))
    return resize(image, shape, data_format="channels_last")


def labels_to_input_size(labels, input_shape):
    """One label image (B, H, W, 1) back to `input_shape`'s H x W when it
    differs -- ONE compartment at a time: the toolbox resamples a single
    channel with OpenCV INTER_NEAREST, but two stacked channels with skimage
    order 0, and the two rules give different pixels."""
    labels = np.asarray(labels)
    if labels.shape[1:-1] == tuple(input_shape[1:-1]):
        return labels
    from deepcell_toolbox.utils import resize
    return resize(labels, tuple(input_shape[1:-1]), data_format="channels_last", labeled_image=True)
