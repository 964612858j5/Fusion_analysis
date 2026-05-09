"""Shared segmentation method configuration helpers.

This module standardizes method names and keeps the existing flat
cellpose_params.json format usable by normalizing it to
cellpose_wholecell_fusion internally.
"""

from copy import deepcopy


CELLPOSE_WHOLECELL_FUSION = "cellpose_wholecell_fusion"
CELLPOSE_NUCLEI_DAPI = "cellpose_nuclei_dapi"
STARDIST_NUCLEI_DAPI = "stardist_nuclei_dapi"
STARDIST_NUCLEI_EXPANSION = "stardist_nuclei_expansion"

SEGMENTATION_METHODS = {
    CELLPOSE_WHOLECELL_FUSION: {
        "method": CELLPOSE_WHOLECELL_FUSION,
        "display_name": "Cellpose whole-cell (Fusion + DAPI)",
        "input_type": "fused_channel_plus_dapi",
        "params": {
            "model_type": "cpsam",
            "diameter": None,
            "flow_threshold": 0.4,
            "cellprob_threshold": 0.0,
            "min_size": 15,
        },
        "output_type": "cell_mask",
    },
    CELLPOSE_NUCLEI_DAPI: {
        "method": CELLPOSE_NUCLEI_DAPI,
        "display_name": "Cellpose nuclei (DAPI)",
        "input_type": "dapi_only",
        "params": {
            "model_type": "cpsam",
            "diameter": None,
            "flow_threshold": 0.4,
            "cellprob_threshold": 0.0,
            "min_size": 15,
        },
        "output_type": "nuclei_mask",
    },
    STARDIST_NUCLEI_DAPI: {
        "method": STARDIST_NUCLEI_DAPI,
        "display_name": "StarDist nuclei (DAPI)",
        "input_type": "dapi_only",
        "params": {
            "model_name": "2D_versatile_fluo",
            "prob_thresh": None,
            "nms_thresh": None,
        },
        "output_type": "nuclei_mask",
    },
    STARDIST_NUCLEI_EXPANSION: {
        "method": STARDIST_NUCLEI_EXPANSION,
        "display_name": "StarDist nuclei + expansion",
        "input_type": "dapi_only",
        "process": "stardist_nuclei_mask_then_skimage_segmentation_expand_labels",
        "params": {
            "model_name": "2D_versatile_fluo",
            "prob_thresh": None,
            "nms_thresh": None,
            "expand_distance": 8,
        },
        "output_type": "expanded_pseudo_cell_mask",
    },
}


def available_segmentation_methods():
    return list(SEGMENTATION_METHODS.keys())


def get_segmentation_method_config(method):
    key = str(method or CELLPOSE_WHOLECELL_FUSION)
    if key not in SEGMENTATION_METHODS:
        raise KeyError(f"Unknown segmentation method: {method}")
    return deepcopy(SEGMENTATION_METHODS[key])


def normalize_segmentation_config(config=None, default_method=CELLPOSE_WHOLECELL_FUSION):
    """Return a standardized config while preserving flat legacy keys.

    Existing code reads params via cfg.get("diameter") etc.  To avoid changing
    worker/UI architecture, normalized configs contain both the standard
    nested "params" block and the same params mirrored at the top level.
    """
    raw = dict(config or {})
    method = raw.get("method") or default_method
    if method not in SEGMENTATION_METHODS:
        method = default_method

    normalized = get_segmentation_method_config(method)
    params = dict(normalized.get("params") or {})
    params.update(dict(raw.get("params") or {}))

    # Legacy flat cellpose_params.json keys.
    for key in (
        "model_type",
        "diameter",
        "flow_threshold",
        "cellprob_threshold",
        "min_size",
        "phase1_diameter",
        "params_source",
    ):
        if key in raw:
            params[key] = raw[key]

    normalized["params"] = params
    for key, value in raw.items():
        if key not in {"method", "display_name", "input_type", "params", "output_type"}:
            normalized[key] = value
    normalized["method"] = method
    normalized["display_name"] = normalized.get("display_name") or SEGMENTATION_METHODS[method]["display_name"]
    normalized["input_type"] = SEGMENTATION_METHODS[method]["input_type"]
    normalized["output_type"] = SEGMENTATION_METHODS[method]["output_type"]

    # Mirror params at top-level for existing code paths.
    normalized.update(params)
    return normalized


def legacy_cellpose_params_to_config(params=None):
    data = dict(params or {})
    data["method"] = CELLPOSE_WHOLECELL_FUSION
    return normalize_segmentation_config(data)
