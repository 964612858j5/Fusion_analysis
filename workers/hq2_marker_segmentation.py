"""
HQ2 nucleus-seeded marker segmentation.

HQ2 keeps Cellpose nuclei as the only seed source, then combines:
  Level 1: existing HQ seeded watershed proposal
  Level 2: ImageJ-style marker proposal
  Level 3: continuous signal expansion from high-confidence cores
"""

import csv
import json
import os

import numpy as np

from .hq_marker_segmentation import (
    parse_hq_channels,
    percentile_normalize,
    segment_nuclei_hq,
)


def _expand_labels(labels, distance):
    try:
        from skimage.segmentation import expand_labels
        return expand_labels(labels, distance=float(distance)).astype(np.uint32, copy=False)
    except Exception:
        from scipy import ndimage as ndi
        labels = np.asarray(labels, dtype=np.uint32)
        if distance <= 0 or labels.max() == 0:
            return labels.copy()
        _, indices = ndi.distance_transform_edt(labels == 0, return_indices=True)
        nearest = labels[tuple(indices)]
        dist = ndi.distance_transform_edt(labels == 0)
        out = nearest.astype(np.uint32, copy=True)
        out[dist > float(distance)] = 0
        return out


def _region_sizes(labels, n_labels=None):
    labels = np.asarray(labels, dtype=np.uint32)
    if n_labels is None:
        n_labels = int(labels.max())
    return np.bincount(labels.ravel(), minlength=n_labels + 1)


def run_level1_hq_proposal(
    nuclei_labels,
    marker_channels,
    channel_names,
    params,
):
    labels, nuclei, qc_rows = segment_nuclei_hq(
        nuclei_labels,
        marker_channels,
        channel_names,
        max_cell_radius=params.get("max_cell_radius", 18),
        normalization_low=params.get("normalization_percentile_low", 1.0),
        normalization_high=params.get("normalization_percentile_high", 99.5),
        consensus_mode=params.get("consensus_mode", "adaptive_best_channel"),
        channel_weights=params.get("channel_weights") or {},
        min_signal_threshold=params.get("min_signal_threshold", 0.08),
    )
    print(f"[HQ2-Level1] hq proposal labels count={int(labels.max())}")
    return labels.astype(np.uint32, copy=False), nuclei, qc_rows


def _imagej_binary(norm, params):
    from scipy import ndimage as ndi
    from skimage import filters, morphology

    img = np.asarray(norm, dtype=np.float32)
    sigma = float(params.get("imagej_blur_sigma", 1.0) or 0)
    if sigma > 0:
        img = ndi.gaussian_filter(img, sigma=sigma)

    bg_radius = int(params.get("imagej_background_radius", 20) or 0)
    if bg_radius > 0:
        try:
            selem = morphology.disk(max(1, bg_radius))
            bg = morphology.opening(img, selem)
            img = np.clip(img - bg, 0.0, 1.0)
        except Exception:
            img = np.clip(img - ndi.grey_opening(img, size=max(1, bg_radius)), 0.0, 1.0)

    method = str(params.get("imagej_threshold_method", "adaptive") or "adaptive").lower()
    if method == "otsu":
        vals = img[np.isfinite(img)]
        thr = filters.threshold_otsu(vals) if vals.size else 1.0
        binary = img >= thr
    elif method == "percentile":
        pct = float(params.get("imagej_threshold_percentile", 75.0) or 75.0)
        vals = img[img > 0]
        thr = np.percentile(vals, pct) if vals.size else 1.0
        binary = img >= thr
    else:
        block = max(15, int(params.get("imagej_background_radius", 20) or 20) * 2 + 1)
        if block % 2 == 0:
            block += 1
        try:
            thr = filters.threshold_local(img, block_size=block, offset=-0.01)
            binary = img >= thr
        except Exception:
            vals = img[img > 0]
            thr = np.percentile(vals, 70) if vals.size else 1.0
            binary = img >= thr

    min_size = int(params.get("imagej_min_object_size", 20) or 0)
    if min_size > 0:
        binary = morphology.remove_small_objects(binary.astype(bool), min_size=min_size)
    open_r = int(params.get("imagej_opening_radius", 1) or 0)
    close_r = int(params.get("imagej_closing_radius", 2) or 0)
    if open_r > 0:
        binary = morphology.binary_opening(binary, morphology.disk(open_r))
    if close_r > 0:
        binary = morphology.binary_closing(binary, morphology.disk(close_r))
    binary = ndi.binary_fill_holes(binary)
    return np.asarray(binary, dtype=bool), img


def run_level2_imagej_style_proposal(
    nuclei_labels,
    marker_channels,
    channel_names,
    params,
):
    from skimage.segmentation import watershed

    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    if n_labels == 0:
        return np.zeros_like(nuclei), []

    bounded = _expand_labels(nuclei, params.get("max_cell_radius", 18))
    candidates = []
    support = np.zeros((n_labels + 1,), dtype=np.float32)

    norm_low = params.get("normalization_percentile_low", 1.0)
    norm_high = params.get("normalization_percentile_high", 99.5)
    for name, arr in zip(channel_names, marker_channels):
        norm = percentile_normalize(arr, norm_low, norm_high)
        binary, processed = _imagej_binary(norm, params)
        mask = (binary & (bounded > 0)) | (nuclei > 0)
        labels = watershed(-processed.astype(np.float32), markers=nuclei, mask=mask)
        labels = labels.astype(np.uint32, copy=False)
        labels[bounded == 0] = 0
        candidates.append(labels)
        for lab in range(1, n_labels + 1):
            region = labels == lab
            if np.any(region):
                support[lab] += float(np.mean(norm[region]))

    final = np.zeros_like(nuclei, dtype=np.uint32)
    for lab in range(1, n_labels + 1):
        claim = np.zeros(nuclei.shape, dtype=bool)
        for labels in candidates:
            claim |= labels == lab
        claim &= bounded == lab
        claim[nuclei == lab] = True
        final[claim] = lab
    print(f"[HQ2-Level2] imagej proposal labels count={int(final.max())}")
    return final, support


def build_high_confidence_core(nuclei_labels, hq_labels, imagej_labels, params):
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    hq = np.asarray(hq_labels, dtype=np.uint32)
    imagej = np.asarray(imagej_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    mode = str(params.get("core_mode", "weighted_support") or "weighted_support")
    min_area = int(params.get("min_core_area", 8) or 0)
    core = np.zeros_like(nuclei, dtype=np.uint32)

    for lab in range(1, n_labels + 1):
        inter = (hq == lab) & (imagej == lab)
        if mode == "intersection":
            region = inter
        elif mode == "majority_support":
            region = ((hq == lab).astype(np.uint8) + (imagej == lab).astype(np.uint8)) >= 1
        else:
            region = inter | ((hq == lab) & (nuclei == 0)) | ((imagej == lab) & (nuclei == 0))
        if np.count_nonzero(region) < min_area:
            region = (hq == lab)
        if np.count_nonzero(region) < min_area:
            region = (imagej == lab)
        if np.count_nonzero(region) < min_area:
            region = (nuclei == lab)
        region[nuclei == lab] = True
        core[region] = lab
    print(f"[HQ2-Core] core cells count={int(core.max())}")
    return core


def build_signal_map(marker_channels, channel_names, params, reference_labels=None):
    norm_low = params.get("normalization_percentile_low", 1.0)
    norm_high = params.get("normalization_percentile_high", 99.5)
    names = list(channel_names or [])
    weights = {name: 1.0 for name in names}
    weights.update(dict(params.get("channel_weights") or {}))
    norm = []
    for name, arr in zip(names, marker_channels):
        n = percentile_normalize(arr, norm_low, norm_high)
        if str(params.get("signal_map_mode", "per_cell_best_channel")) == "weighted_max":
            n = n * float(weights.get(name, 1.0))
        norm.append(n.astype(np.float32, copy=False))
    if not norm:
        raise ValueError("HQ2 requires at least one marker channel.")
    mode = str(params.get("signal_map_mode", "per_cell_best_channel") or "per_cell_best_channel")
    if mode == "per_cell_best_channel" and reference_labels is not None:
        ref = np.asarray(reference_labels, dtype=np.uint32)
        bounded = _expand_labels(ref, params.get("max_expansion_radius", params.get("max_cell_radius", 18)))
        signal = np.maximum.reduce(norm).astype(np.float32, copy=False)
        for lab in range(1, int(ref.max()) + 1):
            nucleus_region = ref == lab
            if not np.any(nucleus_region):
                continue
            best_idx = int(np.argmax([float(np.mean(arr[nucleus_region])) for arr in norm]))
            signal[bounded == lab] = norm[best_idx][bounded == lab]
    else:
        signal = np.maximum.reduce(norm).astype(np.float32, copy=False)
    return signal, dict(zip(names, norm))


def continuous_signal_expansion(
    nuclei_labels,
    hq_labels,
    imagej_labels,
    core_labels,
    signal_map,
    params,
):
    from scipy import ndimage as ndi
    from skimage import filters
    from skimage.segmentation import watershed

    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    if n_labels == 0:
        empty = np.zeros_like(nuclei)
        return empty, empty, {"candidate_pixels": 0, "added_pixels": 0, "conflict_pixels": 0}

    radius = float(params.get("max_expansion_radius", params.get("max_cell_radius", 18)) or 18)
    macrophage_channels = set(parse_hq_channels(params.get("macrophage_channels") or []))
    if macrophage_channels:
        radius = max(radius, float(params.get("macrophage_max_radius", radius) or radius))
    bounded = _expand_labels(nuclei, radius)
    support = ((hq_labels > 0).astype(np.float32) + (imagej_labels > 0).astype(np.float32)) * 0.15
    distance = ndi.distance_transform_edt(nuclei == 0).astype(np.float32)
    gradient = filters.sobel(np.asarray(signal_map, dtype=np.float32)).astype(np.float32)

    min_signal = float(params.get("min_continuous_signal", 0.08) or 0.08)
    candidate = (bounded > 0) & (signal_map >= min_signal)
    candidate |= core_labels > 0
    candidate |= nuclei > 0
    candidate_pixels = int(np.count_nonzero(candidate))

    distance_penalty = float(params.get("distance_penalty_weight", 0.02) or 0.0)
    boundary_weight = float(params.get("boundary_gradient_weight", 0.25) or 0.0)
    score = signal_map + support - distance_penalty * distance - boundary_weight * gradient
    labels = watershed(-score.astype(np.float32), markers=np.asarray(core_labels, dtype=np.uint32), mask=candidate)
    labels = labels.astype(np.uint32, copy=False)
    labels[bounded == 0] = 0

    for lab in range(1, n_labels + 1):
        labels[nuclei == lab] = lab
    labels[(nuclei > 0) & (labels != nuclei)] = nuclei[(nuclei > 0) & (labels != nuclei)]

    hq_claim = hq_labels > 0
    imagej_claim = imagej_labels > 0
    conflict = hq_claim & imagej_claim & (hq_labels != imagej_labels)
    expansion = np.where((labels > 0) & (core_labels == 0), labels, 0).astype(np.uint32)
    stats = {
        "candidate_pixels": candidate_pixels,
        "added_pixels": int(np.count_nonzero(expansion)),
        "conflict_pixels": int(np.count_nonzero(conflict)),
        "blocked_by_boundary": int(np.count_nonzero(candidate & (gradient > np.percentile(gradient, 95)))),
        "blocked_by_neighbor": int(np.count_nonzero(conflict)),
    }
    print(f"[HQ2-Expansion] candidate pixels={stats['candidate_pixels']}")
    print(f"[HQ2-Expansion] added pixels={stats['added_pixels']}")
    print(f"[HQ2-Conflict] conflict pixels={stats['conflict_pixels']}")
    return labels, expansion, stats


def resolve_hq2_conflicts(final_labels, nuclei_labels):
    final = np.asarray(final_labels, dtype=np.uint32).copy()
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    for lab in range(1, n_labels + 1):
        final[nuclei == lab] = lab
        if not np.any(final == lab):
            final[nuclei == lab] = lab
    return final


def compute_hq2_qc(
    nuclei_labels,
    hq_labels,
    imagej_labels,
    core_labels,
    final_labels,
    expansion_labels,
    signal_map,
    channel_names,
    norm_by_name,
    stats,
):
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    final = np.asarray(final_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    nuc_area = _region_sizes(nuclei, n_labels)
    hq_area = _region_sizes(hq_labels, n_labels)
    imagej_area = _region_sizes(imagej_labels, n_labels)
    core_area = _region_sizes(core_labels, n_labels)
    final_area = _region_sizes(final, n_labels)
    expansion_area = _region_sizes(expansion_labels, n_labels)

    rows = []
    low_conf = 0
    for lab in range(1, n_labels + 1):
        region = final == lab
        sig_vals = np.asarray(signal_map[region], dtype=np.float32) if np.any(region) else np.array([], dtype=np.float32)
        main_channel = ""
        main_score = -1.0
        for name in channel_names:
            arr = norm_by_name.get(name)
            if arr is None or not np.any(region):
                continue
            score = float(np.mean(arr[region]))
            if score > main_score:
                main_score = score
                main_channel = name
        ratio = float(final_area[lab]) / max(float(nuc_area[lab]), 1.0)
        low_reason = []
        if core_area[lab] <= 0:
            low_reason.append("empty_core")
        if final_area[lab] < nuc_area[lab]:
            low_reason.append("cell_smaller_than_nucleus")
        if sig_vals.size and float(sig_vals.mean()) < 0.05:
            low_reason.append("low_signal")
        low = bool(low_reason)
        low_conf += int(low)
        rows.append({
            "cell_id": lab,
            "nucleus_area": int(nuc_area[lab]),
            "hq_area": int(hq_area[lab]),
            "imagej_area": int(imagej_area[lab]),
            "core_area": int(core_area[lab]),
            "final_cell_area": int(final_area[lab]),
            "expansion_added_area": int(expansion_area[lab]),
            "cell_to_nucleus_ratio": ratio,
            "main_channel": main_channel,
            "hq_support_score": float(hq_area[lab]) / max(float(final_area[lab]), 1.0),
            "imagej_support_score": float(imagej_area[lab]) / max(float(final_area[lab]), 1.0),
            "core_support_fraction": float(core_area[lab]) / max(float(final_area[lab]), 1.0),
            "continuous_signal_mean": float(sig_vals.mean()) if sig_vals.size else 0.0,
            "continuous_signal_min": float(sig_vals.min()) if sig_vals.size else 0.0,
            "conflict_pixel_count": int(stats.get("conflict_pixels", 0)),
            "expansion_blocked_by_boundary_count": int(stats.get("blocked_by_boundary", 0)),
            "expansion_blocked_by_neighbor_count": int(stats.get("blocked_by_neighbor", 0)),
            "low_confidence_flag": low,
            "low_confidence_reason": ";".join(low_reason),
        })
    print(f"[HQ2-QC] low confidence cells={low_conf}")
    return rows


def run_hq2_segmentation(nuclei_labels, marker_channels, channel_names, params=None):
    params = dict(params or {})
    print("[HQ2] started")
    print(f"[HQ2] hq_channels={list(channel_names or [])}")
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    print(f"[HQ2] nuclei labels count={int(nuclei.max())}")
    hq_labels, nuclei, _hq_qc = run_level1_hq_proposal(nuclei, marker_channels, channel_names, params)
    imagej_labels, _imagej_support = run_level2_imagej_style_proposal(nuclei, marker_channels, channel_names, params)
    core_labels = build_high_confidence_core(nuclei, hq_labels, imagej_labels, params)
    signal_map, norm_by_name = build_signal_map(marker_channels, channel_names, params, reference_labels=nuclei)
    final_labels, expansion_labels, stats = continuous_signal_expansion(
        nuclei, hq_labels, imagej_labels, core_labels, signal_map, params
    )
    final_labels = resolve_hq2_conflicts(final_labels, nuclei)
    qc_rows = compute_hq2_qc(
        nuclei, hq_labels, imagej_labels, core_labels, final_labels, expansion_labels,
        signal_map, channel_names, norm_by_name, stats,
    )
    print(f"[HQ2] final labels count={int(final_labels.max())}")
    return {
        "final_labels": final_labels.astype(np.uint32, copy=False),
        "nuclei_labels": nuclei.astype(np.uint32, copy=False),
        "hq_proposal_labels": hq_labels.astype(np.uint32, copy=False),
        "imagej_proposal_labels": imagej_labels.astype(np.uint32, copy=False),
        "high_confidence_core_labels": core_labels.astype(np.uint32, copy=False),
        "expansion_added_pixels": expansion_labels.astype(np.uint32, copy=False),
        "qc_rows": qc_rows,
        "stats": stats,
    }


HQ2_QC_FIELDS = [
    "cell_id",
    "nucleus_area",
    "hq_area",
    "imagej_area",
    "core_area",
    "final_cell_area",
    "expansion_added_area",
    "cell_to_nucleus_ratio",
    "main_channel",
    "hq_support_score",
    "imagej_support_score",
    "core_support_fraction",
    "continuous_signal_mean",
    "continuous_signal_min",
    "conflict_pixel_count",
    "expansion_blocked_by_boundary_count",
    "expansion_blocked_by_neighbor_count",
    "low_confidence_flag",
    "low_confidence_reason",
]


def write_hq2_qc_table(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HQ2_QC_FIELDS)
        writer.writeheader()
        for row in rows or []:
            writer.writerow({k: row.get(k, "") for k in HQ2_QC_FIELDS})


def hq2_metadata_fields(params, paths):
    return {
        "method": "cellpose_nuclei_hq2",
        "display_name": "Cellpose nuclei + HQ2",
        "nuclei_method": "cellpose_nuclei_dapi",
        "hq_channels": parse_hq_channels(params.get("hq_channels") or []),
        "hq_input_mode": params.get("hq_input_mode", "selected_channels_from_source"),
        "level1_hq_parameters": {
            "max_cell_radius": params.get("max_cell_radius", 18),
            "normalization_percentile_low": params.get("normalization_percentile_low", 1.0),
            "normalization_percentile_high": params.get("normalization_percentile_high", 99.5),
            "consensus_mode": params.get("consensus_mode", "adaptive_best_channel"),
            "min_signal_threshold": params.get("min_signal_threshold", 0.08),
        },
        "level2_imagej_style_parameters": {
            "imagej_blur_sigma": params.get("imagej_blur_sigma", 1.0),
            "imagej_background_radius": params.get("imagej_background_radius", 20),
            "imagej_threshold_method": params.get("imagej_threshold_method", "adaptive"),
            "imagej_threshold_percentile": params.get("imagej_threshold_percentile", 75.0),
            "imagej_min_object_size": params.get("imagej_min_object_size", 20),
            "imagej_closing_radius": params.get("imagej_closing_radius", 2),
            "imagej_opening_radius": params.get("imagej_opening_radius", 1),
        },
        "level3_continuous_expansion_parameters": {
            "core_mode": params.get("core_mode", "weighted_support"),
            "min_core_area": params.get("min_core_area", 8),
            "signal_map_mode": params.get("signal_map_mode", "per_cell_best_channel"),
            "min_continuous_signal": params.get("min_continuous_signal", 0.08),
            "max_expansion_radius": params.get("max_expansion_radius", 25),
            "boundary_gradient_weight": params.get("boundary_gradient_weight", 0.25),
            "distance_penalty_weight": params.get("distance_penalty_weight", 0.02),
            "neighbor_nucleus_penalty_weight": params.get("neighbor_nucleus_penalty_weight", 0.15),
            "allow_irregular_shape": params.get("allow_irregular_shape", True),
            "macrophage_channels": params.get("macrophage_channels", "CD68;CD206"),
            "macrophage_max_radius": params.get("macrophage_max_radius", 35),
            "macrophage_min_signal": params.get("macrophage_min_signal", 0.08),
        },
        "nuclei_mask_path": paths.get("nuclei_mask_path", ""),
        "hq_proposal_mask_path": paths.get("hq_proposal_mask_path", ""),
        "imagej_proposal_mask_path": paths.get("imagej_proposal_mask_path", ""),
        "core_mask_path": paths.get("core_mask_path", ""),
        "expansion_mask_path": paths.get("expansion_mask_path", ""),
        "final_cell_mask_path": paths.get("final_cell_mask_path", ""),
        "qc_table_path": paths.get("qc_table_path", ""),
    }


def save_hq2_outputs(output_dir, prefix, result, params):
    """Save patch/synthetic HQ2 outputs as npy/csv/json files."""
    os.makedirs(output_dir, exist_ok=True)
    paths = {}
    mapping = {
        "nuclei_mask_path": "nuclei_labels",
        "hq_proposal_mask_path": "hq_proposal_labels",
        "imagej_proposal_mask_path": "imagej_proposal_labels",
        "core_mask_path": "high_confidence_core_labels",
        "expansion_mask_path": "expansion_added_pixels",
        "final_cell_mask_path": "final_labels",
    }
    for path_key, result_key in mapping.items():
        path = os.path.join(output_dir, f"{prefix}_{result_key}.npy")
        np.save(path, np.asarray(result[result_key], dtype=np.uint32))
        paths[path_key] = os.path.abspath(path)
    qc_path = os.path.join(output_dir, f"{prefix}_hq2_qc_table.csv")
    write_hq2_qc_table(qc_path, result.get("qc_rows") or [])
    paths["qc_table_path"] = os.path.abspath(qc_path)
    meta = hq2_metadata_fields(params, paths)
    meta_path = os.path.join(output_dir, f"{prefix}_segmentation_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"[HQ2] outputs saved to={output_dir}")
    return paths, meta_path
