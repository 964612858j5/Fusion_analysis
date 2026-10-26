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
# CHANGES: format_output_mesmer, the default post-processing arguments of
# Mesmer.predict and the per-compartment branch of mesmer_postprocess.
"""From the four heads to label images: Mesmer's formatting (inner distance
+ the interior class of the pixelwise softmax per compartment) and
deepcell_toolbox `deep_watershed` with Mesmer's defaults, each compartment
on its own."""

DEFAULTS = {
    "whole-cell": {"maxima_threshold": 0.075, "maxima_smooth": 0, "interior_threshold": 0.2,
                   "interior_smooth": 2, "small_objects_threshold": 15,
                   "fill_holes_threshold": 15, "radius": 2},
    "nuclear": {"maxima_threshold": 0.1, "maxima_smooth": 0, "interior_threshold": 0.2,
                "interior_smooth": 2, "small_objects_threshold": 15,
                "fill_holes_threshold": 15, "radius": 2},
}


def format_outputs(heads):
    if len(heads) != 4:
        raise ValueError(f"expected the 4 Mesmer heads, got {len(heads)}")
    return {"whole-cell": [heads[0], heads[1][..., 1:2]],
            "nuclear": [heads[2], heads[3][..., 1:2]]}


def segment(formatted, compartment, overrides=None):
    """Label image (B, H, W, 1) of `compartment`; `overrides` replace the
    defaults key by key."""
    from deepcell_toolbox.deep_watershed import deep_watershed
    if compartment not in DEFAULTS:
        raise ValueError(f"Invalid compartment supplied: {compartment}")
    return deep_watershed(formatted[compartment], **{**DEFAULTS[compartment], **dict(overrides or {})})
