"""Shared segmentation method configuration helpers.

This module standardizes method names and keeps the existing flat
cellpose_params.json format usable by normalizing it to
cellpose_wholecell_fusion internally.
"""

from copy import deepcopy


CELLPOSE_WHOLECELL_FUSION = "cellpose_wholecell_fusion"
CELLPOSE_NUCLEI_DAPI = "cellpose_nuclei_dapi"
CELLPOSE_NUCLEI_EXPANSION = "cellpose_nuclei_expansion"
CELLPOSE_NUCLEI_HQ = "cellpose_nuclei_hq"
CELLPOSE_NUCLEI_HQ2 = "cellpose_nuclei_hq2"
STARDIST_NUCLEI_DAPI = "stardist_nuclei_dapi"
STARDIST_NUCLEI_EXPANSION = "stardist_nuclei_expansion"
MESMER_WHOLE_CELL = "mesmer_whole_cell"
MESMER_NUCLEI = "mesmer_nuclei"
MESMER_NUCLEAR_GUIDED = "mesmer_nuclear_guided"

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
            "use_gpu": True,
            "tile_size": 1024,
            "batch_size": 8,
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
            "use_gpu": True,
            "tile_size": 1024,
            "batch_size": 8,
        },
        "output_type": "nuclei_mask",
    },
    CELLPOSE_NUCLEI_EXPANSION: {
        "method": CELLPOSE_NUCLEI_EXPANSION,
        "display_name": "Cellpose nuclei (DAPI) + expansion",
        "input_type": "dapi_only",
        "process": "cellpose_nuclei_mask_then_skimage_segmentation_expand_labels",
        "params": {
            "model_type": "cpsam",
            "diameter": None,
            "flow_threshold": 0.4,
            "cellprob_threshold": 0.0,
            "min_size": 15,
            "expand_distance": 8,
            "use_gpu": True,
            "tile_size": 1024,
            "batch_size": 8,
        },
        "output_type": "expanded_pseudo_cell_mask",
    },
    CELLPOSE_NUCLEI_HQ: {
        "method": CELLPOSE_NUCLEI_HQ,
        "display_name": "Cellpose nuclei + HQ",
        "input_type": "dapi_plus_hq_structural_channels",
        "process": "cellpose_nuclei_then_hq_seeded_watershed_consensus",
        "params": {
            "model_type": "cpsam",
            "diameter": None,
            "flow_threshold": 0.4,
            "cellprob_threshold": 0.0,
            "min_size": 15,
            "use_gpu": True,
            "tile_size": 1024,
            "batch_size": 8,
            "hq_channels": [],
            "hq_input_mode": "selected_channels_from_source",
            "max_cell_radius": 12,
            "normalization_percentile_low": 1.0,
            "normalization_percentile_high": 99.5,
            "consensus_mode": "adaptive_best_channel",
            "channel_weights": {},
            "min_signal_threshold": 0.08,
        },
        "output_type": "whole_cell_mask",
    },
    CELLPOSE_NUCLEI_HQ2: {
        "method": CELLPOSE_NUCLEI_HQ2,
        "display_name": "Cellpose nuclei + HQ2",
        "input_type": "dapi_plus_hq_structural_channels",
        "process": "cellpose_nuclei_then_hq2_multialgorithm_continuous_expansion",
        "params": {
            "model_type": "cpsam",
            "diameter": None,
            "flow_threshold": 0.4,
            "cellprob_threshold": 0.0,
            "min_size": 15,
            "use_gpu": True,
            "tile_size": 1024,
            "batch_size": 8,
            "hq_channels": [],
            "hq_input_mode": "selected_channels_from_source",
            "max_cell_radius": 18,
            "normalization_percentile_low": 1.0,
            "normalization_percentile_high": 99.5,
            "consensus_mode": "adaptive_best_channel",
            "channel_weights": {},
            "min_signal_threshold": 0.08,
            "imagej_blur_sigma": 1.0,
            "imagej_background_radius": 20,
            "imagej_threshold_method": "adaptive",
            "imagej_threshold_percentile": 75.0,
            "imagej_min_object_size": 20,
            "imagej_closing_radius": 2,
            "imagej_opening_radius": 1,
            "core_mode": "weighted_support",
            "min_core_area": 8,
            "signal_map_mode": "per_cell_best_channel",
            "min_continuous_signal": 0.08,
            "max_expansion_radius": 25,
            "boundary_gradient_weight": 0.25,
            "distance_penalty_weight": 0.02,
            "neighbor_nucleus_penalty_weight": 0.15,
            "allow_irregular_shape": True,
            "macrophage_channels": "CD68;CD206",
            "macrophage_max_radius": 35,
            "macrophage_min_signal": 0.08,
        },
        "output_type": "whole_cell_mask",
    },
    STARDIST_NUCLEI_DAPI: {
        "method": STARDIST_NUCLEI_DAPI,
        "display_name": "StarDist nuclei (DAPI)",
        "input_type": "dapi_only",
        "params": {
            "model_name": "2D_versatile_fluo",
            "prob_thresh": None,
            "nms_thresh": None,
            "device_preference": "gpu_first",
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
            "device_preference": "gpu_first",
        },
        "output_type": "expanded_pseudo_cell_mask",
    },
    MESMER_WHOLE_CELL: {
        "method": MESMER_WHOLE_CELL,
        "display_name": "Mesmer whole-cell",
        "input_type": "dapi_plus_membrane",
        "params": {
            "mesmer_mode": "whole_cell",
            "nuclear_channel": "DAPI",
            "membrane_channels": [],
            "cytoplasm_channel": "",
            "input_mode": "selected_channels",
            "compartment": "whole-cell",
            "image_mpp": 0.5,
            "pixel_size": 0.5,
            "batch_size": 1,
            "tile_size": 2048,
            "overlap": 128,
            "use_gpu": "auto",
            "normalize_input": True,
            "percentile_low": 1.0,
            "percentile_high": 99.8,
            "postprocess_min_size": 0,
            "fill_holes": False,
            "remove_border_objects": False,
            "save_intermediate": False,
            "channel_weights": {},
        },
        "output_type": "whole_cell_mask",
    },
    MESMER_NUCLEI: {
        "method": MESMER_NUCLEI,
        "display_name": "Mesmer nuclei",
        "input_type": "dapi_only",
        "params": {
            "mesmer_mode": "nuclei",
            "nuclear_channel": "DAPI",
            "membrane_channels": [],
            "cytoplasm_channel": "",
            "input_mode": "DAPI only",
            "compartment": "nuclear",
            "image_mpp": 0.5,
            "pixel_size": 0.5,
            "batch_size": 1,
            "tile_size": 2048,
            "overlap": 128,
            "use_gpu": "auto",
            "normalize_input": True,
            "percentile_low": 1.0,
            "percentile_high": 99.8,
            "postprocess_min_size": 0,
            "fill_holes": False,
            "remove_border_objects": False,
            "save_intermediate": False,
            "channel_weights": {},
        },
        "output_type": "nuclei_mask",
    },
    MESMER_NUCLEAR_GUIDED: {
        "method": MESMER_NUCLEAR_GUIDED,
        "display_name": "Mesmer nuclear-guided whole-cell",
        "input_type": "dapi_plus_membrane",
        "process": "mesmer_whole_cell_with_nuclear_mask_side_output",
        "params": {
            "mesmer_mode": "nuclear_guided",
            "nuclear_channel": "DAPI",
            "membrane_channels": [],
            "cytoplasm_channel": "",
            "input_mode": "selected_channels",
            "compartment": "both",
            "image_mpp": 0.5,
            "pixel_size": 0.5,
            "batch_size": 1,
            "tile_size": 2048,
            "overlap": 128,
            "use_gpu": "auto",
            "normalize_input": True,
            "percentile_low": 1.0,
            "percentile_high": 99.8,
            "postprocess_min_size": 0,
            "fill_holes": False,
            "remove_border_objects": False,
            "save_intermediate": False,
            "channel_weights": {},
        },
        "output_type": "whole_cell_mask",
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
        "use_gpu",
        "tile_size",
        "batch_size",
        "expand_distance",
        "phase1_diameter",
        "params_source",
        "hq_channels",
        "hq_input_mode",
        "max_cell_radius",
        "normalization_percentile_low",
        "normalization_percentile_high",
        "consensus_mode",
        "channel_weights",
        "min_signal_threshold",
        "imagej_blur_sigma",
        "imagej_background_radius",
        "imagej_threshold_method",
        "imagej_threshold_percentile",
        "imagej_min_object_size",
        "imagej_closing_radius",
        "imagej_opening_radius",
        "core_mode",
        "min_core_area",
        "signal_map_mode",
        "min_continuous_signal",
        "max_expansion_radius",
        "boundary_gradient_weight",
        "distance_penalty_weight",
        "neighbor_nucleus_penalty_weight",
        "allow_irregular_shape",
        "macrophage_channels",
        "macrophage_max_radius",
        "macrophage_min_signal",
        "mesmer_mode",
        "nuclear_channel",
        "membrane_channels",
        "cytoplasm_channel",
        "input_mode",
        "compartment",
        "image_mpp",
        "pixel_size",
        "overlap",
        "normalize_input",
        "percentile_low",
        "percentile_high",
        "postprocess_min_size",
        "fill_holes",
        "remove_border_objects",
        "save_intermediate",
        "enable_step2_profiling",
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
    if "process" in SEGMENTATION_METHODS[method]:
        normalized["process"] = SEGMENTATION_METHODS[method]["process"]

    # Mirror params at top-level for existing code paths.
    normalized.update(params)
    return normalized


def legacy_cellpose_params_to_config(params=None):
    data = dict(params or {})
    data["method"] = CELLPOSE_WHOLECELL_FUSION
    return normalize_segmentation_config(data)
