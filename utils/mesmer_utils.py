"""Mesmer helpers of the application: the method names, the membrane
channel list, the mask post-processing and the run's metadata.

Mesmer itself runs in the seg_runner engine (a PyTorch port,
seg_runner/mesmer_torch); the in-process DeepCell / Keras path was removed
(Windows plan W2).
"""

from __future__ import annotations

import numpy as np


MESMER_WHOLE_CELL = "mesmer_whole_cell"
MESMER_NUCLEI = "mesmer_nuclei"
MESMER_NUCLEAR_GUIDED = "mesmer_nuclear_guided"
MESMER_METHODS = (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED)

MESMER_DISPLAY_NAMES = {
    MESMER_WHOLE_CELL: "Mesmer whole-cell",
    MESMER_NUCLEI: "Mesmer nuclei",
    MESMER_NUCLEAR_GUIDED: "Mesmer nuclear-guided whole-cell",
}


def parse_channel_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.replace(",", ";").split(";") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


def postprocess_mask(mask, min_size=0, fill_holes=False, remove_border_objects=False):
    mask = np.asarray(mask, dtype=np.uint32)
    if min_size and int(min_size) > 0:
        from skimage.morphology import remove_small_objects

        mask = remove_small_objects(mask, min_size=int(min_size)).astype(np.uint32)
    if fill_holes:
        from scipy import ndimage as ndi
        from skimage.measure import label

        mask = label(ndi.binary_fill_holes(mask > 0)).astype(np.uint32)
    if remove_border_objects and mask.size:
        border = set(np.unique(mask[0, :])) | set(np.unique(mask[-1, :])) | set(np.unique(mask[:, 0])) | set(np.unique(mask[:, -1]))
        border.discard(0)
        if border:
            out = mask.copy()
            for lab in border:
                out[out == lab] = 0
            mask = out
    return mask.astype(np.uint32, copy=False)


def mesmer_metadata(method, params, device_status, output_mask_path="", runtime_seconds=None, extra=None):
    data = {
        "method": method,
        "display_name": MESMER_DISPLAY_NAMES.get(method, method),
        "device_requested": str(params.get("use_gpu", "auto")),
        "device_used": device_status.device_used if hasattr(device_status, "device_used") else "",
        "tensorflow_version": getattr(device_status, "tensorflow_version", ""),
        "deepcell_version": getattr(device_status, "deepcell_version", ""),
        "nuclear_channel": params.get("nuclear_channel", "DAPI"),
        "membrane_channels": parse_channel_list(params.get("membrane_channels")),
        "input_mode": params.get("input_mode", "selected_channels"),
        "weights": params.get("channel_weights") or params.get("weights") or {},
        "normalization_percentiles": [
            params.get("percentile_low", params.get("normalization_percentile_low", 1.0)),
            params.get("percentile_high", params.get("normalization_percentile_high", 99.8)),
        ],
        "tile_size": params.get("tile_size"),
        "overlap": params.get("overlap"),
        "output_mask_path": output_mask_path,
        "whole_cell_mask_path": output_mask_path if method != MESMER_NUCLEI else "",
        "nuclei_mask_path": params.get("nuclei_mask_path", ""),
        "runtime_seconds": runtime_seconds,
    }
    if extra:
        data.update(extra)
    return data
