"""
HQ2 nucleus-seeded marker segmentation.

HQ2 keeps Cellpose nuclei as the only seed source, then combines:
  Level 1: HQ multi-channel seeded watershed proposal
  Level 2: conservative boundary refinement near the HQ mask edge

The older ImageJ-style proposal and large continuous expansion helpers remain
available as experimental functions, but the default HQ2 path does not call
them.
"""

import csv
import json
import os
import time

import numpy as np

from .hq_marker_segmentation import (
    parse_hq_channels,
    percentile_normalize,
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


class HQ2SafetyFallback(RuntimeError):
    """Raised when HQ2 must stop an expensive stage and use a safe fallback."""


def _default_safety(params):
    p = dict(params or {})
    p.setdefault("enable_refinement", True)
    p.setdefault("hq2_expansion_engine", "conservative")
    p.setdefault("enable_imagej_proposal", False)
    p.setdefault("max_refine_radius", 6)
    p.setdefault("max_candidate_pixels_per_cell", 3000)
    p.setdefault("max_added_area_fraction", 0.35)
    p.setdefault("max_cell_to_nucleus_ratio", 10.0)
    p.setdefault("min_refine_signal", 0.08)
    p.setdefault("refine_signal_mad_factor", 1.5)
    p.setdefault("prevent_crossing_neighbor_nuclei", True)
    p.setdefault("protect_other_cell_core", True)
    p.setdefault("fallback_to_hq_if_overexpanded", True)
    p.setdefault("timeout_seconds", 600)
    p.setdefault("memory_guard_mb", 0)
    return p


def _elapsed(started):
    return time.perf_counter() - started


def _rss_mb():
    try:
        import psutil
        return float(psutil.Process(os.getpid()).memory_info().rss) / (1024.0 * 1024.0)
    except Exception:
        return 0.0


def _check_safety(params, total_started, cancel_check=None, stage="HQ2"):
    if cancel_check is not None and cancel_check():
        raise HQ2SafetyFallback(f"{stage} cancelled")
    timeout = float(params.get("timeout_seconds", 600) or 0)
    if timeout > 0 and _elapsed(total_started) > timeout:
        raise HQ2SafetyFallback(f"{stage} exceeded timeout_seconds={timeout:g}")
    guard = float(params.get("memory_guard_mb", 0) or 0)
    rss = _rss_mb()
    if guard > 0 and rss > guard:
        raise HQ2SafetyFallback(f"{stage} exceeded memory_guard_mb={guard:g} (rss={rss:.1f} MB)")


def _emit_progress(progress_callback, message):
    if progress_callback is not None:
        try:
            progress_callback(str(message))
        except Exception:
            pass


def run_level1_hq_proposal(
    nuclei_labels,
    marker_channels,
    channel_names,
    params,
):
    from skimage.segmentation import watershed

    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    if n_labels == 0:
        return np.zeros_like(nuclei), nuclei.copy(), []
    bounded = _expand_labels(nuclei, params.get("max_cell_radius", 18))
    norm_low = params.get("normalization_percentile_low", 1.0)
    norm_high = params.get("normalization_percentile_high", 99.5)
    min_signal = float(params.get("min_signal_threshold", 0.08) or 0.08)
    weights = {name: 1.0 for name in (channel_names or [])}
    weights.update(dict(params.get("channel_weights") or {}))

    candidates = []
    scores = []
    for name, arr in zip(channel_names, marker_channels):
        norm = percentile_normalize(arr, norm_low, norm_high)
        mask = (bounded > 0) & (norm >= min_signal)
        mask[nuclei > 0] = True
        labels = watershed(-norm.astype(np.float32), markers=nuclei, mask=mask)
        labels = labels.astype(np.uint32, copy=False)
        labels[bounded == 0] = 0
        candidates.append(labels)
        flat = labels.ravel()
        counts = np.bincount(flat, minlength=n_labels + 1).astype(np.float32)
        sums = np.bincount(flat, weights=norm.ravel(), minlength=n_labels + 1).astype(np.float32)
        scores.append((sums / np.maximum(counts, 1.0)) * float(weights.get(name, 1.0)))

    if not candidates:
        labels = nuclei.copy()
    else:
        score_arr = np.vstack(scores)
        best_channel = np.argmax(score_arr, axis=0)
        labels = np.zeros_like(nuclei, dtype=np.uint32)
        bounded_positive = bounded > 0
        for idx, cand in enumerate(candidates):
            pix = (cand > 0) & bounded_positive & (best_channel[cand] == idx)
            labels[pix] = cand[pix]
        labels[nuclei > 0] = nuclei[nuclei > 0]
    print(f"[HQ2-Level1] hq proposal labels count={int(labels.max())}")
    return labels.astype(np.uint32, copy=False), nuclei, []


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
    support = np.zeros((n_labels + 1,), dtype=np.float32)
    final_supported = np.zeros(nuclei.shape, dtype=bool)

    norm_low = params.get("normalization_percentile_low", 1.0)
    norm_high = params.get("normalization_percentile_high", 99.5)
    for name, arr in zip(channel_names, marker_channels):
        norm = percentile_normalize(arr, norm_low, norm_high)
        binary, processed = _imagej_binary(norm, params)
        mask = (binary & (bounded > 0)) | (nuclei > 0)
        labels = watershed(-processed.astype(np.float32), markers=nuclei, mask=mask)
        labels = labels.astype(np.uint32, copy=False)
        labels[bounded == 0] = 0
        flat_l = labels.ravel()
        counts = np.bincount(flat_l, minlength=n_labels + 1).astype(np.float32)
        sums = np.bincount(flat_l, weights=norm.ravel(), minlength=n_labels + 1).astype(np.float32)
        support += sums / np.maximum(counts, 1.0)
        final_supported |= (labels > 0) & (labels == bounded)

    final = np.where(final_supported, bounded, 0).astype(np.uint32, copy=False)
    final[nuclei > 0] = nuclei[nuclei > 0]
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

    if mode == "intersection":
        inter = (hq > 0) & (hq == imagej)
        core[inter] = hq[inter]
    elif mode == "majority_support":
        core[hq > 0] = hq[hq > 0]
        fill = (core == 0) & (imagej > 0)
        core[fill] = imagej[fill]
    else:
        inter = (hq > 0) & (hq == imagej)
        core[inter] = hq[inter]
        hq_extra = (core == 0) & (hq > 0) & (nuclei == 0)
        core[hq_extra] = hq[hq_extra]
        imagej_extra = (core == 0) & (imagej > 0) & (nuclei == 0)
        core[imagej_extra] = imagej[imagej_extra]

    if min_area > 0:
        core_area = _region_sizes(core, n_labels)
        low_core = core_area <= min_area
        hq_fill = (core == 0) & (hq > 0) & low_core[hq]
        core[hq_fill] = hq[hq_fill]
        core_area = _region_sizes(core, n_labels)
        low_core = core_area <= min_area
        imagej_fill = (core == 0) & (imagej > 0) & low_core[imagej]
        core[imagej_fill] = imagej[imagej_fill]
    core[nuclei > 0] = nuclei[nuclei > 0]
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
    if mode in {"max", "max_fusion"}:
        signal = np.maximum.reduce(norm).astype(np.float32, copy=False)
    elif mode == "per_cell_best_channel" and reference_labels is not None:
        ref = np.asarray(reference_labels, dtype=np.uint32)
        n_labels = int(ref.max())
        bounded = _expand_labels(ref, params.get("max_cell_radius", 18))
        signal = np.maximum.reduce(norm).astype(np.float32, copy=False)
        flat_ref = ref.ravel()
        counts = np.bincount(flat_ref, minlength=n_labels + 1).astype(np.float32)
        scores = []
        for arr in norm:
            sums = np.bincount(flat_ref, weights=arr.ravel(), minlength=n_labels + 1).astype(np.float32)
            scores.append(sums / np.maximum(counts, 1.0))
        best_idx_by_label = np.argmax(np.vstack(scores), axis=0).astype(np.int16)
        bounded_positive = bounded > 0
        for idx, arr in enumerate(norm):
            pix = bounded_positive & (best_idx_by_label[bounded] == idx)
            signal[pix] = arr[pix]
    else:
        signal = np.maximum.reduce(norm).astype(np.float32, copy=False)
    return signal, dict(zip(names, norm))


def _disk_structure(radius):
    r = max(1, int(radius))
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    return (yy * yy + xx * xx) <= (r * r)


def _expanded_slice(slc, shape, pad):
    if slc is None:
        return None
    y, x = slc
    y0 = max(0, int(y.start) - int(pad))
    y1 = min(shape[0], int(y.stop) + int(pad))
    x0 = max(0, int(x.start) - int(pad))
    x1 = min(shape[1], int(x.stop) + int(pad))
    return (slice(y0, y1), slice(x0, x1))


def _robust_threshold(values, min_signal, mad_factor):
    vals = np.asarray(values, dtype=np.float32)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return float(min_signal)
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med)))
    return max(float(min_signal), med + float(mad_factor) * mad)


def run_conservative_refinement(
    initial_labels,
    nuclei_labels,
    signal_map,
    norm_by_name,
    channel_names,
    params,
    total_started=None,
    progress_callback=None,
    cancel_check=None,
):
    from scipy import ndimage as ndi
    try:
        from skimage import filters
        gradient = filters.sobel(np.asarray(signal_map, dtype=np.float32)).astype(np.float32)
    except Exception:
        gradient = np.zeros_like(signal_map, dtype=np.float32)

    initial = np.asarray(initial_labels, dtype=np.uint32)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    final = initial.copy()
    added = np.zeros_like(initial, dtype=np.uint32)
    n_labels = int(max(initial.max(), nuclei.max()))
    if n_labels == 0:
        return final, added, {"rows": [], "summary": {"cells_refined": 0, "cells_fallback": 0, "added_pixels_total": 0}}

    enabled = bool(params.get("enable_refinement", True))
    engine = str(params.get("hq2_expansion_engine", "conservative") or "conservative").lower()
    if not enabled or engine == "disabled":
        rows = []
        nuc_area = _region_sizes(nuclei, n_labels)
        init_area = _region_sizes(initial, n_labels)
        for lab in range(1, n_labels + 1):
            rows.append({
                "cell_id": lab,
                "nucleus_area": int(nuc_area[lab]),
                "initial_hq_area": int(init_area[lab]),
                "refined_area": int(init_area[lab]),
                "added_area": 0,
                "added_area_fraction": 0.0,
                "cell_to_nucleus_ratio": float(init_area[lab]) / max(float(nuc_area[lab]), 1.0),
                "main_channel": "",
                "local_threshold": 0.0,
                "mean_refine_signal": 0.0,
                "conflict_pixel_count": 0,
                "refinement_applied": False,
                "fallback_to_hq": False,
                "low_confidence_flag": False,
                "low_confidence_reason": "",
            })
        return final, added, {"rows": rows, "summary": {"cells_refined": 0, "cells_fallback": 0, "added_pixels_total": 0}}
    if engine not in {"conservative", "fast_watershed"}:
        raise HQ2SafetyFallback(f"HQ2 expansion engine {engine!r} is not enabled by default")

    radius = int(params.get("max_refine_radius", 6) or 0)
    if radius <= 0:
        radius = 1
    max_cell_radius = float(params.get("max_cell_radius", 18) or 18)
    max_candidates = int(params.get("max_candidate_pixels_per_cell", 3000) or 0)
    max_added_fraction = float(params.get("max_added_area_fraction", 0.35) or 0.35)
    max_ratio = float(params.get("max_cell_to_nucleus_ratio", 10.0) or 10.0)
    min_signal = float(params.get("min_refine_signal", 0.08) or 0.08)
    mad_factor = float(params.get("refine_signal_mad_factor", 1.5) or 1.5)
    fallback_over = bool(params.get("fallback_to_hq_if_overexpanded", True))
    prevent_nuclei = bool(params.get("prevent_crossing_neighbor_nuclei", True))
    protect_core = bool(params.get("protect_other_cell_core", True))
    max_grad = params.get("max_boundary_gradient")
    max_grad = None if max_grad in (None, "") else float(max_grad)

    structure = _disk_structure(radius)
    bounded_nuclei = _expand_labels(nuclei, max_cell_radius)
    initial_area = _region_sizes(initial, n_labels)
    nucleus_area = _region_sizes(nuclei, n_labels)
    object_slices = ndi.find_objects(initial, max_label=n_labels)
    rows = []
    cells_refined = 0
    cells_fallback = 0
    overexpanded = 0
    conflict_total = 0

    for lab in range(1, n_labels + 1):
        if total_started is not None:
            _check_safety(params, total_started, cancel_check, stage="HQ2-Refinement")
        slc = object_slices[lab - 1] if lab - 1 < len(object_slices) else None
        init_area = int(initial_area[lab]) if lab < len(initial_area) else 0
        nuc_area = int(nucleus_area[lab]) if lab < len(nucleus_area) else 0
        row = {
            "cell_id": lab,
            "nucleus_area": nuc_area,
            "initial_hq_area": init_area,
            "refined_area": init_area,
            "added_area": 0,
            "added_area_fraction": 0.0,
            "cell_to_nucleus_ratio": float(init_area) / max(float(nuc_area), 1.0),
            "main_channel": "",
            "local_threshold": 0.0,
            "mean_refine_signal": 0.0,
            "conflict_pixel_count": 0,
            "refinement_applied": False,
            "fallback_to_hq": False,
            "low_confidence_flag": False,
            "low_confidence_reason": "",
        }
        if slc is None or init_area <= 0:
            rows.append(row)
            continue
        local_slc = _expanded_slice(slc, initial.shape, radius + 1)
        local_initial = initial[local_slc]
        local_nuclei = nuclei[local_slc]
        local_signal = np.asarray(signal_map[local_slc], dtype=np.float32)
        local_gradient = gradient[local_slc]
        local_bounded = bounded_nuclei[local_slc]
        cell_mask = local_initial == lab
        if not np.any(cell_mask):
            rows.append(row)
            continue
        dilated = ndi.binary_dilation(cell_mask, structure=structure)
        ring = dilated & ~cell_mask
        if prevent_nuclei:
            ring &= ~((local_nuclei > 0) & (local_nuclei != lab))
        ring &= local_bounded == lab
        if protect_core:
            ring &= (local_initial == 0) | (local_initial == lab)
        if not np.any(ring):
            rows.append(row)
            continue
        local_threshold = _robust_threshold(local_signal[ring], min_signal, mad_factor)
        candidate = ring & (local_signal >= local_threshold)
        if max_grad is not None:
            candidate &= local_gradient <= max_grad
        candidate_count = int(np.count_nonzero(candidate))
        if max_candidates > 0 and candidate_count > max_candidates:
            row["fallback_to_hq"] = True
            row["low_confidence_flag"] = True
            row["low_confidence_reason"] = "candidate_pixels_limit_refinement_fallback"
            row["local_threshold"] = float(local_threshold)
            cells_fallback += 1
            rows.append(row)
            continue

        # Only keep candidate components touching the original cell mask.
        allowed = candidate | cell_mask
        comp, _n_comp = ndi.label(allowed)
        touching = np.unique(comp[cell_mask])
        touching = touching[touching > 0]
        if touching.size:
            candidate &= np.isin(comp, touching)
        else:
            candidate[:] = False

        conflict = candidate & (local_initial > 0) & (local_initial != lab)
        conflict_count = int(np.count_nonzero(conflict))
        if conflict_count:
            candidate &= ~conflict
        added_count = int(np.count_nonzero(candidate))
        added_fraction = float(added_count) / max(float(init_area), 1.0)
        refined_area = init_area + added_count
        ratio = float(refined_area) / max(float(nuc_area), 1.0)
        over = (
            ratio > max_ratio
            or added_fraction > max_added_fraction
            or (max_candidates > 0 and added_count > max_candidates)
        )
        if over and fallback_over:
            row["fallback_to_hq"] = True
            row["low_confidence_flag"] = True
            row["low_confidence_reason"] = "overexpanded_refinement_fallback"
            overexpanded += 1
            cells_fallback += 1
            added_count = 0
            added_fraction = 0.0
            refined_area = init_area
            ratio = float(init_area) / max(float(nuc_area), 1.0)
            candidate[:] = False
        elif added_count > 0:
            final_local = final[local_slc]
            added_local = added[local_slc]
            final_local[candidate] = lab
            added_local[candidate] = lab
            cells_refined += 1

        main_channel = ""
        main_score = -1.0
        for name in channel_names:
            arr = norm_by_name.get(name)
            if arr is None:
                continue
            vals = np.asarray(arr[local_slc], dtype=np.float32)[cell_mask | candidate]
            score = float(vals.mean()) if vals.size else 0.0
            if score > main_score:
                main_score = score
                main_channel = name
        conflict_total += conflict_count
        row.update({
            "refined_area": int(refined_area),
            "added_area": int(added_count),
            "added_area_fraction": float(added_fraction),
            "cell_to_nucleus_ratio": float(ratio),
            "main_channel": main_channel,
            "local_threshold": float(local_threshold),
            "mean_refine_signal": float(local_signal[candidate].mean()) if added_count else 0.0,
            "conflict_pixel_count": conflict_count,
            "refinement_applied": bool(added_count > 0),
        })
        rows.append(row)

    final[nuclei > 0] = nuclei[nuclei > 0]
    summary = {
        "cells_refined": int(cells_refined),
        "cells_fallback": int(cells_fallback),
        "overexpanded_cell_count": int(overexpanded),
        "added_pixels_total": int(np.count_nonzero(added)),
        "conflict_pixels_total": int(conflict_total),
        "max_refine_radius": int(radius),
    }
    _emit_progress(
        progress_callback,
        f"[HQ2] conservative refinement cells refined={cells_refined} fallback={cells_fallback} added={summary['added_pixels_total']}",
    )
    return final.astype(np.uint32, copy=False), added.astype(np.uint32, copy=False), {"rows": rows, "summary": summary}


def continuous_signal_expansion(
    nuclei_labels,
    hq_labels,
    imagej_labels,
    core_labels,
    signal_map,
    params,
    total_started=None,
    progress_callback=None,
    cancel_check=None,
):
    from scipy import ndimage as ndi
    from skimage import filters
    from skimage.segmentation import watershed

    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    if n_labels == 0:
        empty = np.zeros_like(nuclei)
        return empty, empty, {"candidate_pixels": 0, "added_pixels": 0, "conflict_pixels": 0}

    engine = str(params.get("hq2_expansion_engine", "fast_watershed") or "fast_watershed").lower()
    if engine == "disabled":
        stats = {
            "candidate_pixels": 0,
            "added_pixels": 0,
            "conflict_pixels": 0,
            "blocked_by_boundary": 0,
            "blocked_by_neighbor": 0,
            "iterations": 0,
            "engine": engine,
            "fallback": True,
            "fallback_reason": "HQ2 expansion disabled",
        }
        return hq_labels.astype(np.uint32, copy=True), np.zeros_like(nuclei), stats
    if engine not in {"fast_watershed", "priority_queue"}:
        engine = "fast_watershed"
    if engine == "priority_queue":
        raise HQ2SafetyFallback("HQ2 priority_queue expansion is experimental and disabled by safe mode")

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
    max_candidates = int(params.get("max_total_candidate_pixels", 2_000_000) or 0)
    if max_candidates > 0 and candidate_pixels > max_candidates:
        raise HQ2SafetyFallback(
            f"HQ2 expansion candidate pixels {candidate_pixels:,} exceeded max_total_candidate_pixels={max_candidates:,}"
        )
    max_pixels_per_cell = int(params.get("max_pixels_per_cell", 5000) or 0)
    if max_pixels_per_cell > 0:
        candidate_area = _region_sizes(np.where(candidate, bounded, 0), n_labels)
        largest = int(np.max(candidate_area[1:])) if candidate_area.size > 1 else 0
        if largest > max_pixels_per_cell:
            raise HQ2SafetyFallback(
                f"HQ2 expansion candidate pixels per cell {largest:,} exceeded max_pixels_per_cell={max_pixels_per_cell:,}"
            )
    if total_started is not None:
        _check_safety(params, total_started, cancel_check, stage="HQ2-Expansion")
    _emit_progress(progress_callback, f"[HQ2-Expansion] candidate pixels={candidate_pixels:,}")

    distance_penalty = float(params.get("distance_penalty_weight", 0.02) or 0.0)
    boundary_weight = float(params.get("boundary_gradient_weight", 0.25) or 0.0)
    score = signal_map + support - distance_penalty * distance - boundary_weight * gradient
    labels = watershed(-score.astype(np.float32), markers=np.asarray(core_labels, dtype=np.uint32), mask=candidate)
    labels = labels.astype(np.uint32, copy=False)
    labels[bounded == 0] = 0

    labels[nuclei > 0] = nuclei[nuclei > 0]

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
        "iterations": 1,
        "engine": engine,
        "fallback": False,
        "fallback_reason": "",
    }
    print(f"[HQ2-Expansion] candidate pixels={stats['candidate_pixels']}")
    print(f"[HQ2-Expansion] added pixels={stats['added_pixels']}")
    print(f"[HQ2-Conflict] conflict pixels={stats['conflict_pixels']}")
    return labels, expansion, stats


def resolve_hq2_conflicts(final_labels, nuclei_labels):
    final = np.asarray(final_labels, dtype=np.uint32).copy()
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    final[nuclei > 0] = nuclei[nuclei > 0]
    return final


def compute_hq2_qc(
    nuclei_labels,
    initial_hq_labels,
    final_labels,
    refinement_added_labels,
    signal_map,
    channel_names,
    norm_by_name,
    refine_info,
):
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    final = np.asarray(final_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    nuc_area = _region_sizes(nuclei, n_labels)
    hq_area = _region_sizes(initial_hq_labels, n_labels)
    final_area = _region_sizes(final, n_labels)
    added_area = _region_sizes(refinement_added_labels, n_labels)
    signal = np.asarray(signal_map, dtype=np.float32)
    flat_final = final.ravel()
    main_channel_by_label = np.full(n_labels + 1, "", dtype=object)
    main_score = np.full(n_labels + 1, -1.0, dtype=np.float32)
    for name in channel_names:
        arr = norm_by_name.get(name)
        if arr is None:
            continue
        sums = np.bincount(
            flat_final,
            weights=np.asarray(arr, dtype=np.float32).ravel(),
            minlength=n_labels + 1,
        ).astype(np.float32)
        means = sums / np.maximum(final_area, 1)
        better = means > main_score
        main_score[better] = means[better]
        main_channel_by_label[better] = name

    refine_rows = {
        int(row.get("cell_id", 0) or 0): dict(row)
        for row in (refine_info or {}).get("rows", [])
    }
    rows = []
    low_conf = 0
    for lab in range(1, n_labels + 1):
        ratio = float(final_area[lab]) / max(float(nuc_area[lab]), 1.0)
        rr = refine_rows.get(lab, {})
        low_reason = []
        if final_area[lab] < nuc_area[lab]:
            low_reason.append("cell_smaller_than_nucleus")
        if rr.get("low_confidence_reason"):
            low_reason.append(str(rr.get("low_confidence_reason")))
        low = bool(low_reason)
        low_conf += int(low)
        add_area = int(added_area[lab]) if lab < len(added_area) else 0
        rows.append({
            "cell_id": lab,
            "nucleus_area": int(nuc_area[lab]),
            "initial_hq_area": int(hq_area[lab]),
            "refined_area": int(final_area[lab]),
            "added_area": add_area,
            "added_area_fraction": float(add_area) / max(float(hq_area[lab]), 1.0),
            "cell_to_nucleus_ratio": ratio,
            "main_channel": str(rr.get("main_channel") or main_channel_by_label[lab] or ""),
            "local_threshold": float(rr.get("local_threshold", 0.0) or 0.0),
            "mean_refine_signal": float(rr.get("mean_refine_signal", 0.0) or 0.0),
            "conflict_pixel_count": int(rr.get("conflict_pixel_count", 0) or 0),
            "refinement_applied": bool(rr.get("refinement_applied", False)),
            "fallback_to_hq": bool(rr.get("fallback_to_hq", False)),
            "low_confidence_flag": low,
            "low_confidence_reason": ";".join(low_reason),
        })
    print(f"[HQ2-QC] low confidence cells={low_conf}")
    return rows


def _log_stage(logger, message):
    if logger is not None:
        logger.info(message)
    else:
        print(message)


def run_hq2_segmentation(nuclei_labels, marker_channels, channel_names, params=None,
                         logger=None, return_layers=True, progress_callback=None,
                         cancel_check=None):
    params = _default_safety(params)
    total_started = time.perf_counter()
    timings = {}
    warnings = []
    _log_stage(logger, "[HQ2] started")
    _log_stage(logger, f"[HQ2] hq_channels={list(channel_names or [])}")
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    _log_stage(logger, f"[HQ2] input shape={tuple(nuclei.shape)}")
    _log_stage(logger, f"[HQ2] nuclei labels count={int(nuclei.max())}")
    _log_stage(logger, f"[HQ2] mode=conservative_refinement")
    _emit_progress(progress_callback, f"[HQ2] input shape={tuple(nuclei.shape)} nuclei={int(nuclei.max())}")

    stage_started = time.perf_counter()
    timings["run_nuclei_seconds"] = 0.0
    _log_stage(logger, "[HQ2] run nuclei seconds=0.00")

    stage_started = time.perf_counter()
    hq_labels, nuclei, _hq_qc = run_level1_hq_proposal(nuclei, marker_channels, channel_names, params)
    timings["level1_hq_proposal_seconds"] = _elapsed(stage_started)
    timings["run_hq_proposal_seconds"] = timings["level1_hq_proposal_seconds"]
    _log_stage(logger, f"[HQ2] run HQ proposal seconds={timings['run_hq_proposal_seconds']:.2f}")
    _check_safety(params, total_started, cancel_check, stage="HQ2-Level1")

    stage_started = time.perf_counter()
    signal_map, norm_by_name = build_signal_map(marker_channels, channel_names, params, reference_labels=hq_labels)
    timings["signal_map_seconds"] = _elapsed(stage_started)
    _log_stage(logger, f"[HQ2-Signal] end seconds={timings['signal_map_seconds']:.2f}")
    _check_safety(params, total_started, cancel_check, stage="HQ2-Signal")

    stage_started = time.perf_counter()
    try:
        final_labels, refinement_added, refine_info = run_conservative_refinement(
            hq_labels, nuclei, signal_map, norm_by_name, channel_names, params,
            total_started=total_started,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
    except (HQ2SafetyFallback, MemoryError) as exc:
        warning = f"HQ2 refinement exceeded safety limit; falling back to HQ proposal: {exc}"
        warnings.append(warning)
        _log_stage(logger, f"[HQ2] warning: {warning}")
        _emit_progress(progress_callback, f"[HQ2] warning: {warning}")
        final_labels = hq_labels.astype(np.uint32, copy=True)
        refinement_added = np.zeros_like(nuclei, dtype=np.uint32)
        refine_info = {
            "rows": [],
            "summary": {
                "cells_refined": 0,
                "cells_fallback": int(nuclei.max()),
                "overexpanded_cell_count": 0,
                "added_pixels_total": 0,
                "conflict_pixels_total": 0,
                "fallback_reason": str(exc),
            },
        }
    timings["conservative_refinement_seconds"] = _elapsed(stage_started)
    summary = dict(refine_info.get("summary") or {})
    _log_stage(logger, f"[HQ2] conservative refinement seconds={timings['conservative_refinement_seconds']:.2f}")
    _log_stage(logger, f"[HQ2] cells refined={int(summary.get('cells_refined', 0))}")
    _log_stage(logger, f"[HQ2] cells fallback={int(summary.get('cells_fallback', 0))}")
    _log_stage(logger, f"[HQ2] added pixels total={int(summary.get('added_pixels_total', 0))}")

    stage_started = time.perf_counter()
    final_labels = resolve_hq2_conflicts(final_labels, nuclei)
    timings["conflict_resolution_seconds"] = _elapsed(stage_started)
    _log_stage(logger, f"[HQ2-Conflict] seconds={timings['conflict_resolution_seconds']:.2f}")

    stage_started = time.perf_counter()
    qc_rows = compute_hq2_qc(
        nuclei, hq_labels, final_labels, refinement_added,
        signal_map, channel_names, norm_by_name, refine_info,
    )
    timings["qc_seconds"] = _elapsed(stage_started)
    timings["total_seconds"] = _elapsed(total_started)
    peak_mb = _rss_mb()
    _log_stage(logger, f"[HQ2-QC] seconds={timings['qc_seconds']:.2f}")
    _log_stage(logger, f"[HQ2] peak memory estimate={peak_mb:.1f} MB")
    _log_stage(logger, f"[HQ2] final labels count={int(final_labels.max())}")
    _log_stage(logger, f"[HQ2 profile] total={timings['total_seconds']:.2f}s")

    result = {
        "final_labels": final_labels.astype(np.uint32, copy=False),
        "nuclei_labels": nuclei.astype(np.uint32, copy=False),
        "qc_rows": qc_rows,
        "stats": summary,
        "metadata": {
            "timings": timings,
            "fallback": bool(warnings) or int(summary.get("cells_fallback", 0)) > 0,
            "fallback_reason": "; ".join(warnings),
            "warnings": warnings,
            "peak_memory_estimate_mb": peak_mb,
            "hq2_mode": "conservative_refinement",
            "imagej_proposal_enabled": False,
            "fallback_count": int(summary.get("cells_fallback", 0)),
            "overexpanded_cell_count": int(summary.get("overexpanded_cell_count", 0)),
            "refinement_added_pixels": int(summary.get("added_pixels_total", 0)),
            "expansion_engine": params.get("hq2_expansion_engine", "conservative"),
        },
    }
    if return_layers:
        zeros = np.zeros_like(nuclei, dtype=np.uint32)
        result.update({
            "hq_proposal_labels": hq_labels.astype(np.uint32, copy=False),
            "imagej_proposal_labels": zeros,
            "high_confidence_core_labels": hq_labels.astype(np.uint32, copy=False),
            "expansion_added_pixels": refinement_added.astype(np.uint32, copy=False),
            "refinement_added_pixels": refinement_added.astype(np.uint32, copy=False),
        })
    return result


HQ2_QC_FIELDS = [
    "cell_id",
    "nucleus_area",
    "initial_hq_area",
    "refined_area",
    "added_area",
    "added_area_fraction",
    "cell_to_nucleus_ratio",
    "main_channel",
    "local_threshold",
    "mean_refine_signal",
    "conflict_pixel_count",
    "refinement_applied",
    "fallback_to_hq",
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
    runtime = dict(params.get("hq2_runtime_metadata") or {})
    refinement_params = {
        "enable_refinement": params.get("enable_refinement", True),
        "max_refine_radius": params.get("max_refine_radius", 6),
        "signal_map_mode": params.get("signal_map_mode", "per_cell_best_channel"),
        "min_refine_signal": params.get("min_refine_signal", 0.08),
        "refine_signal_mad_factor": params.get("refine_signal_mad_factor", 1.5),
        "max_added_area_fraction": params.get("max_added_area_fraction", 0.35),
        "max_cell_to_nucleus_ratio": params.get("max_cell_to_nucleus_ratio", 10.0),
        "max_candidate_pixels_per_cell": params.get("max_candidate_pixels_per_cell", 3000),
        "prevent_crossing_neighbor_nuclei": params.get("prevent_crossing_neighbor_nuclei", True),
        "protect_other_cell_core": params.get("protect_other_cell_core", True),
        "fallback_to_hq_if_overexpanded": params.get("fallback_to_hq_if_overexpanded", True),
        "timeout_seconds": params.get("timeout_seconds", 600),
        "memory_guard_mb": params.get("memory_guard_mb", 0),
    }
    return {
        "method": "cellpose_nuclei_hq2",
        "display_name": "Cellpose nuclei + HQ2",
        "hq2_mode": "conservative_refinement",
        "imagej_proposal_enabled": bool(params.get("enable_imagej_proposal", False)),
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
        "experimental_imagej_style_parameters": {
            "enabled": bool(params.get("enable_imagej_proposal", False)),
            "imagej_blur_sigma": params.get("imagej_blur_sigma", 1.0),
            "imagej_background_radius": params.get("imagej_background_radius", 20),
            "imagej_threshold_method": params.get("imagej_threshold_method", "adaptive"),
            "imagej_threshold_percentile": params.get("imagej_threshold_percentile", 75.0),
            "imagej_min_object_size": params.get("imagej_min_object_size", 20),
            "imagej_closing_radius": params.get("imagej_closing_radius", 2),
            "imagej_opening_radius": params.get("imagej_opening_radius", 1),
        },
        "refinement_params": refinement_params,
        "fallback_count": int(runtime.get("fallback_count", 0) or 0),
        "overexpanded_cell_count": int(runtime.get("overexpanded_cell_count", 0) or 0),
        "runtime_by_stage": runtime.get("timings", {}),
        "nuclei_mask_path": paths.get("nuclei_mask_path", ""),
        "initial_hq_mask_path": paths.get("initial_hq_mask_path") or paths.get("hq_proposal_mask_path", ""),
        "hq_proposal_mask_path": paths.get("hq_proposal_mask_path", ""),
        "imagej_proposal_mask_path": paths.get("imagej_proposal_mask_path", ""),
        "core_mask_path": paths.get("core_mask_path", ""),
        "expansion_mask_path": paths.get("expansion_mask_path", ""),
        "refinement_added_pixels_path": paths.get("refinement_added_pixels_path") or paths.get("expansion_mask_path", ""),
        "final_cell_mask_path": paths.get("final_cell_mask_path", ""),
        "qc_table_path": paths.get("qc_table_path", ""),
        "hq2_runtime_metadata": runtime,
    }


def save_hq2_outputs(output_dir, prefix, result, params):
    """Save patch/synthetic HQ2 outputs as npy/csv/json files."""
    os.makedirs(output_dir, exist_ok=True)
    paths = {}
    mapping = {
        "nuclei_mask_path": "nuclei_labels",
        "hq_proposal_mask_path": "hq_proposal_labels",
        "initial_hq_mask_path": "hq_proposal_labels",
        "imagej_proposal_mask_path": "imagej_proposal_labels",
        "core_mask_path": "high_confidence_core_labels",
        "expansion_mask_path": "expansion_added_pixels",
        "refinement_added_pixels_path": "expansion_added_pixels",
        "final_cell_mask_path": "final_labels",
    }
    for path_key, result_key in mapping.items():
        path = os.path.join(output_dir, f"{prefix}_{result_key}.npy")
        np.save(path, np.asarray(result[result_key], dtype=np.uint32))
        paths[path_key] = os.path.abspath(path)
    qc_path = os.path.join(output_dir, f"{prefix}_hq2_qc_table.csv")
    write_hq2_qc_table(qc_path, result.get("qc_rows") or [])
    paths["qc_table_path"] = os.path.abspath(qc_path)
    meta_params = dict(params or {})
    if result.get("metadata"):
        meta_params["hq2_runtime_metadata"] = result.get("metadata")
    meta = hq2_metadata_fields(meta_params, paths)
    meta_path = os.path.join(output_dir, f"{prefix}_segmentation_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"[HQ2] outputs saved to={output_dir}")
    return paths, meta_path
