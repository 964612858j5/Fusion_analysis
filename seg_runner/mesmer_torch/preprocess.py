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
# CHANGES: deepcell.applications.mesmer.mesmer_preprocess with its default
# arguments fixed; the operations are still deepcell_toolbox's.
"""Mesmer's default preprocessing, through deepcell_toolbox: on a copy, each
image and channel clipped at the 99.9th percentile of its non-zero pixels,
then histogram-normalised (rescale to [0, 1], CLAHE with kernel 128)."""
import numpy as np

PERCENTILE = 99.9
KERNEL_SIZE = 128


def mesmer_preprocess(image):
    from deepcell_toolbox.processing import histogram_normalization, percentile_threshold
    if image.ndim != 4:
        raise ValueError(f"Image data must be 4D, got image of shape {image.shape}")
    out = percentile_threshold(image=np.copy(image), percentile=PERCENTILE)
    return histogram_normalization(image=out, kernel_size=KERNEL_SIZE)
