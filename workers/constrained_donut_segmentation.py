"""Constrained Donut Segmentation (CDS) nucleus-owned cytoplasm segmentation.

The key invariant is that nuclei define cell territory. Marker signal may move
interfaces between neighboring territories, but threshold/gating never decides
whether cytoplasm exists.
"""

import csv
import json
import os
import time

import numpy as np
from scipy import ndimage as _scipy_ndi

try:
    import cupy as _cupy
    from cupyx.scipy import ndimage as _cupyx_ndi
    _HAS_CUPY = True
except ImportError:
    _cupy = None
    _cupyx_ndi = None
    _HAS_CUPY = False

from .hq2_marker_segmentation import HQ2_QC_FIELDS, _expand_labels
from .hq_marker_segmentation import parse_hq_channels


DEFAULT_CSD_PARAMS = {
    "minimal_radius": 3,
    "default_lymphocyte_radius": 6,
    "donut_size": 18,
    "max_cell_radius": 18,
    "shrink_pixels": 2,
    "nucleus_shrink": 2,
    "strong_support_threshold": 0.12,
    "weak_hotspot_threshold": 0.8,
    "gi_kernel_size": 7,
    "gi_background_kernel_size": 31,
    "gi_background_mode": "local",
    "gi_specificity_enabled": True,
    "gi_top_k": 0,
    "gi_specificity_floor": 0.05,
    "weak_hotspot_connectivity": 8,
    "max_added_area_fraction": 3.0,
    "max_cell_to_nucleus_ratio": 8.0,
    "lymphocyte_max_cell_to_nucleus_ratio": 4.0,
    "large_cell_max_cell_to_nucleus_ratio": 12.0,
    "enable_weak_hotspot": True,
    "enable_strong_signal_keep": True,
    "support_weight": 1.0,
    "weak_support_weight": 0.5,
    "distance_penalty_weight": 0.03,
    "neighbor_penalty_weight": 0.0,
    "boundary_gradient_weight": 0.25,
    "constraint_low_percentile": 0.5,
    "constraint_high_percentile": 99.5,
    "constraint_mode": "per_cell_best_channel",
    "local_contrast_enabled": False,
    "local_contrast_radius": 15,
    "weak_local_z_threshold": 0.75,
    "weak_min_component_area": 8,
    "bg_sigma_factor": 3.0,
    "saturation_percentile": 99.8,
    "use_gpu": True,
    "timeout_seconds": 600,
    "memory_guard_mb": 0,
}


CSD_QC_FIELDS = list(HQ2_QC_FIELDS) + [
    "marker_support_score",
    "signal_mode",
    "weak_signal_flag",
    "prevented_nucleus_only_collapse",
    "weak_support_mean",
    "support_confidence",
    "base_territory_area",
    "minimal_keep_area",
    "strong_keep_area",
    "weak_hotspot_keep_area",
    "final_area",
    "dominant_channel",
    "dominant_cell_type_hint",
    "gi_score_mean",
    "gi_score_p90",
    "strong_support_area",
    "weak_hotspot_area",
    "unsupported_outer_area_removed",
    "oversegmentation_guard_applied",
]


class CSDFallback(RuntimeError):
    """Raised when CDS must stop because of cancellation or safety limits."""


def _params(params):
    supplied = dict(params or {})
    out = dict(DEFAULT_CSD_PARAMS)
    out.update(supplied)
    if (
        float(out.get("weak_hotspot_threshold", 0.8) or 0.8) == 1.5
        and "gi_specificity_enabled" not in supplied
        and "gi_specificity_floor" not in supplied
        and "gi_top_k" not in supplied
    ):
        out["weak_hotspot_threshold"] = DEFAULT_CSD_PARAMS["weak_hotspot_threshold"]
    if "max_cell_radius" not in supplied and "donut_size" in supplied:
        out["max_cell_radius"] = out.get("donut_size", DEFAULT_CSD_PARAMS["donut_size"])
    if "shrink_pixels" not in supplied and "nucleus_shrink" in out:
        out["shrink_pixels"] = out.get("nucleus_shrink", 3)
    return out


def _asnumpy(arr):
    if _HAS_CUPY and isinstance(arr, _cupy.ndarray):
        return _cupy.asnumpy(arr)
    return np.asarray(arr)


def _emit(progress_callback, message):
    if progress_callback is not None:
        try:
            progress_callback(str(message))
        except Exception:
            pass


def _rss_mb():
    try:
        import psutil
        return float(psutil.Process(os.getpid()).memory_info().rss) / (1024.0 * 1024.0)
    except Exception:
        return 0.0


def _check_safety(params, started, cancel_check=None, stage="CDS"):
    if cancel_check is not None and cancel_check():
        raise CSDFallback(f"{stage} cancelled")
    timeout = float(params.get("timeout_seconds", 600) or 0)
    if timeout > 0 and time.perf_counter() - started > timeout:
        raise CSDFallback(f"{stage} exceeded timeout_seconds={timeout:g}")
    guard = float(params.get("memory_guard_mb", 0) or 0)
    rss = _rss_mb()
    if guard > 0 and rss > guard:
        raise CSDFallback(f"{stage} exceeded memory_guard_mb={guard:g} (rss={rss:.1f} MB)")


def _log(logger, progress_callback, message):
    if logger is not None:
        try:
            logger.info(message)
        except Exception:
            pass
    _emit(progress_callback, message)


def _channel_threshold(raw_np, bg_mask, bg_sigma_factor, saturation_percentile):
    raw = np.asarray(raw_np, dtype=np.float32)
    finite = np.isfinite(raw)
    vals = raw[bg_mask & finite]
    if vals.size < 200:
        all_finite = raw[finite]
        if all_finite.size:
            p10 = float(np.percentile(all_finite, 10))
            vals = raw[finite & (raw <= p10)]
    if vals.size:
        med = float(np.median(vals))
        mad = float(np.median(np.abs(vals - med)))
    else:
        med = 0.0
        mad = 0.0
    sigma = mad * 1.4826
    low = med + float(bg_sigma_factor) * sigma
    if sigma == 0.0:
        low = med * 1.1 + 1.0
    positive = raw[(raw > 0) & finite]
    high = float(np.percentile(positive, float(saturation_percentile))) if positive.size else float("inf")
    if high < low:
        high = float("inf")
    return float(low), float(high), float(med), float(sigma)


def _perimeter(mask):
    try:
        from skimage.measure import perimeter_crofton
        return float(perimeter_crofton(mask.astype(bool), directions=4))
    except Exception:
        eroded = _scipy_ndi.binary_erosion(mask)
        return float(np.count_nonzero(mask & ~eroded))


def _expand_slice(slc, shape, pad):
    out = []
    for s, lim in zip(slc, shape):
        out.append(slice(max(0, s.start - pad), min(lim, s.stop + pad)))
    return tuple(out)


def _safe_percentiles(raw, low_pct, high_pct):
    vals = np.asarray(raw, dtype=np.float32)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return 0.0, 1.0
    lo = float(np.percentile(vals, float(low_pct)))
    hi = float(np.percentile(vals, float(high_pct)))
    if not np.isfinite(hi) or hi <= lo:
        hi = lo + 1.0
    return lo, hi


_CUPY_HOTSPOT_OK = None
_CUPY_HOTSPOT_WARNED = False


def _cupy_hotspot_available():
    global _CUPY_HOTSPOT_OK
    if _CUPY_HOTSPOT_OK is not None:
        return bool(_CUPY_HOTSPOT_OK)
    if not _HAS_CUPY or _cupyx_ndi is None:
        _CUPY_HOTSPOT_OK = False
        return False
    try:
        test = _cupy.ones((8, 8), dtype=_cupy.float32)
        _ = _cupyx_ndi.uniform_filter(test, size=3)
        _cupy.cuda.Stream.null.synchronize()
        _CUPY_HOTSPOT_OK = True
    except Exception:
        _CUPY_HOTSPOT_OK = False
    return bool(_CUPY_HOTSPOT_OK)


def _uniform_filter(arr, size, use_gpu=True):
    global _CUPY_HOTSPOT_WARNED
    arr = np.asarray(arr, dtype=np.float32)
    if use_gpu and _cupy_hotspot_available():
        try:
            arr_x = _cupy.asarray(arr)
            out_x = _cupyx_ndi.uniform_filter(arr_x, size=int(size), mode="nearest")
            return _cupy.asnumpy(out_x).astype(np.float32, copy=False), "cupy"
        except Exception:
            if not _CUPY_HOTSPOT_WARNED:
                _CUPY_HOTSPOT_WARNED = True
    return _scipy_ndi.uniform_filter(arr, size=int(size), mode="nearest").astype(np.float32, copy=False), "scipy"


def _cell_type_hint(channel_name):
    name = str(channel_name or "").upper().replace("-", "").replace("_", "")
    lymph = {"CD3D", "CD3", "CD4", "CD8", "CD8A", "CD8B", "CD45RA", "CD45RO"}
    large = {"HSBAG", "HBSAG", "CK19", "PANCK", "CYTOKERATIN", "KRT", "EPCAM"}
    if name in lymph:
        return "lymphocyte"
    if name in large:
        return "large_epithelial"
    return "unknown"


def _ratio_limit_for_channel(channel_name, params):
    p = _params(params)
    hint = _cell_type_hint(channel_name)
    if hint == "lymphocyte":
        return float(p.get("lymphocyte_max_cell_to_nucleus_ratio", 4.0) or 4.0)
    if hint == "large_epithelial":
        return float(p.get("large_cell_max_cell_to_nucleus_ratio", 12.0) or 12.0)
    return float(p.get("max_cell_to_nucleus_ratio", 8.0) or 8.0)


def build_nucleus_owned_territory(nuclei_labels, params=None):
    """Build marker-independent territory owned by each nucleus."""
    p = _params(params)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    radius = float(p.get("max_cell_radius") or p.get("donut_size") or 30)
    radius = max(0.0, radius)
    territory = _expand_labels(nuclei, radius).astype(np.uint32, copy=False)
    territory[nuclei > 0] = nuclei[nuclei > 0]
    dist = _scipy_ndi.distance_transform_edt(nuclei == 0).astype(np.float32, copy=False)
    return territory, dist


def _choose_dominant_channels(base_territory, norm_maps, channel_names):
    n_labels = int(base_territory.max())
    dominant = {}
    if not norm_maps:
        return {lab: "" for lab in range(1, n_labels + 1)}
    slices = _scipy_ndi.find_objects(base_territory, max_label=n_labels)
    for lab in range(1, n_labels + 1):
        slc = slices[lab - 1] if lab - 1 < len(slices) else None
        if slc is None:
            dominant[lab] = channel_names[0] if channel_names else ""
            continue
        local = base_territory[slc] == lab
        if not np.any(local):
            dominant[lab] = channel_names[0] if channel_names else ""
            continue
        scores = [float(np.mean(m[slc][local])) for m in norm_maps]
        dominant[lab] = channel_names[int(np.argmax(scores))] if channel_names else ""
    return dominant


def build_soft_constraint_maps(nuclei_labels, base_territory, marker_channels, channel_names, params=None):
    """Build raw-strength segmentation signal and normalized support maps separately."""
    p = _params(params)
    shape = base_territory.shape
    marker_channels = [np.asarray(ch, dtype=np.float32) for ch in (marker_channels or [])]
    channel_names = list(channel_names or [])[:len(marker_channels)]
    if not channel_names:
        channel_names = [f"channel_{i + 1}" for i in range(len(marker_channels))]

    low_pct = float(p.get("constraint_low_percentile", 0.5) or 0.5)
    high_pct = float(p.get("constraint_high_percentile", 99.5) or 99.5)
    mode = str(p.get("constraint_mode", "per_cell_best_channel") or "per_cell_best_channel").strip().lower()
    local_contrast = bool(p.get("local_contrast_enabled", False))
    local_radius = max(1, int(p.get("local_contrast_radius", 15) or 15))

    raw_maps = []
    norm_maps = []
    thresholds = {}
    bg_mask = base_territory == 0
    for name, raw in zip(channel_names, marker_channels):
        lo, hi = _safe_percentiles(raw, low_pct, high_pct)
        clipped = np.clip(raw, lo, hi).astype(np.float32, copy=False)
        raw_maps.append(clipped)
        norm = np.clip((clipped - lo) / max(hi - lo, 1.0e-6), 0.0, 1.0).astype(np.float32, copy=False)
        if local_contrast:
            size = local_radius * 2 + 1
            med = _scipy_ndi.median_filter(norm, size=size, mode="nearest")
            mad = _scipy_ndi.median_filter(np.abs(norm - med), size=size, mode="nearest")
            norm = np.clip((norm - med) / (mad * 1.4826 + 1.0e-3), 0.0, 3.0) / 3.0
            norm = norm.astype(np.float32, copy=False)
        norm_maps.append(norm)
        auto_low, auto_high, bg_median, bg_sigma_est = _channel_threshold(
            raw, bg_mask, p.get("bg_sigma_factor", 3.0), p.get("saturation_percentile", 99.8)
        )
        thresholds[name] = {
            "auto_low": float(auto_low),
            "auto_high": float(auto_high),
            "bg_median": float(bg_median),
            "bg_sigma_est": float(bg_sigma_est),
            "clip_low": float(lo),
            "clip_high": float(hi),
        }

    if not norm_maps:
        zeros = np.zeros(shape, dtype=np.float32)
        return zeros.copy(), zeros.copy(), {}, {}, thresholds

    raw_stack = np.stack(raw_maps, axis=0)
    norm_stack = np.stack(norm_maps, axis=0)
    dominant = _choose_dominant_channels(base_territory, norm_maps, channel_names)

    if mode == "weighted_max":
        weights = []
        for m in norm_maps:
            vals = m[base_territory > 0]
            weights.append(float(np.mean(vals)) if vals.size else 1.0)
        weights = np.asarray(weights, dtype=np.float32)
        weights = weights / max(float(weights.sum()), 1.0e-6)
        normalized_support_map = np.max(norm_stack * weights[:, None, None] * len(weights), axis=0)
        segmentation_signal_map = np.max(raw_stack * weights[:, None, None] * len(weights), axis=0)
    elif mode == "max":
        normalized_support_map = np.max(norm_stack, axis=0)
        segmentation_signal_map = np.max(raw_stack, axis=0)
    else:
        normalized_support_map = np.max(norm_stack, axis=0)
        segmentation_signal_map = np.max(raw_stack, axis=0)

    per_channel_norm_maps = {name: norm.astype(np.float32, copy=False) for name, norm in zip(channel_names, norm_maps)}
    return (
        segmentation_signal_map.astype(np.float32, copy=False),
        np.clip(normalized_support_map, 0.0, 1.0).astype(np.float32, copy=False),
        per_channel_norm_maps,
        dominant,
        thresholds,
    )


def build_strong_signal_keep(base_territory, support_map, nuclei_labels, minimal_keep_labels, params=None):
    """Retain explicitly supported cytoplasm inside the upper-bound territory."""
    p = _params(params)
    if not bool(p.get("enable_strong_signal_keep", True)):
        return np.zeros_like(base_territory, dtype=np.uint32)
    base = np.asarray(base_territory, dtype=np.uint32)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    minimal = np.asarray(minimal_keep_labels, dtype=np.uint32)
    support = np.asarray(support_map, dtype=np.float32)
    threshold = float(p.get("strong_support_threshold", 0.12) or 0.12)
    candidate = (support >= threshold) & (base > 0) & (nuclei == 0)
    labels = np.where(candidate, base, 0).astype(np.uint32, copy=False)
    return _keep_connected_to_seed(labels, nuclei, minimal, base)


def _gi_top_k_count(n_channels, params=None):
    p = _params(params)
    fixed = int(p.get("gi_top_k", 0) or 0)
    if fixed > 0:
        return max(1, min(int(n_channels), fixed))
    return max(1, min(3, int(np.ceil(float(n_channels) / 2.0))))


def build_multichannel_gi_star(marker_channels, channel_names, base_territory, params=None):
    """Per-channel Gi* hotspot detection with specificity-weighted scoring."""
    p = _params(params)
    territory_mask = np.asarray(base_territory) > 0
    marker_channels = [np.asarray(raw, dtype=np.float32) for raw in (marker_channels or [])]
    channel_names = list(channel_names or [])[:len(marker_channels)]
    if not channel_names:
        channel_names = [f"channel_{idx + 1}" for idx in range(len(marker_channels))]
    n_channels = len(marker_channels)
    if n_channels == 0 or not np.any(territory_mask):
        shape = np.asarray(base_territory).shape
        return np.zeros(shape, dtype=np.float32), {}, "none"

    k = max(3, int(p.get("gi_kernel_size", 7) or 7))
    if k % 2 == 0:
        k += 1
    bg_k = max(k + 2, int(p.get("gi_background_kernel_size", 31) or 31))
    if bg_k % 2 == 0:
        bg_k += 1
    use_gpu = str(p.get("use_gpu", True)).strip().lower() not in {"0", "false", "no", "off", "cpu"}
    gi_mode = str(p.get("gi_background_mode", "local") or "local").strip().lower()
    backend = "cupy" if (use_gpu and _cupy_hotspot_available()) else "scipy"
    xp = _cupy if backend == "cupy" else np
    xndi = _cupyx_ndi if backend == "cupy" else _scipy_ndi

    try:
        territory_x = xp.asarray(territory_mask)
        gi_stack_list = []
        per_channel_gi = {}
        for name, raw in zip(channel_names, marker_channels):
            raw_x = xp.asarray(raw, dtype=xp.float32)
            local_mean = xndi.uniform_filter(raw_x, size=k, mode="nearest")
            if gi_mode == "global":
                vals = raw_x[territory_x]
                if vals.size:
                    bg_mean = float(_asnumpy(xp.mean(vals)))
                    bg_std = float(_asnumpy(xp.std(vals)))
                else:
                    bg_mean = 0.0
                    bg_std = 1.0
                bg = xp.full_like(raw_x, bg_mean)
                std = xp.full_like(raw_x, bg_std)
            else:
                bg = xndi.uniform_filter(raw_x, size=bg_k, mode="nearest")
                sq = xndi.uniform_filter(raw_x * raw_x, size=bg_k, mode="nearest")
                var = xp.maximum(sq - bg * bg, xp.float32(0.0))
                std = xp.sqrt(var)
            gi = (local_mean - bg) / (std + xp.float32(1.0e-3))
            gi = xp.where(territory_x, gi, xp.float32(0.0))
            gi = xp.where(xp.isfinite(gi), gi, xp.float32(0.0))
            gi = xp.maximum(gi, xp.float32(0.0))
            gi_stack_list.append(gi)
            per_channel_gi[name] = _asnumpy(gi).astype(np.float32, copy=False)

        gi_stack = xp.stack(gi_stack_list, axis=0)
        top_k = _gi_top_k_count(int(gi_stack.shape[0]), p)
        sorted_gi = xp.sort(gi_stack, axis=0)
        top_k_mean = xp.mean(sorted_gi[-top_k:, :, :], axis=0)
        gi_max = xp.max(gi_stack, axis=0)
        gi_mean = xp.mean(gi_stack, axis=0)
        if int(gi_stack.shape[0]) >= 3:
            gi_median = xp.median(gi_stack, axis=0)
        else:
            gi_median = gi_mean
        if int(gi_stack.shape[0]) == 1 or not bool(p.get("gi_specificity_enabled", True)):
            specificity = xp.ones_like(gi_max)
        else:
            specificity = (gi_max - gi_median) / (gi_mean + xp.float32(1.0e-3))
            specificity = xp.float32(1.0) - xp.exp(-specificity)
        composite = (top_k_mean * specificity).astype(xp.float32)
        specificity_floor = float(p.get("gi_specificity_floor", 0.05) or 0.0)
        if specificity_floor > 0 and int(gi_stack.shape[0]) > 1 and bool(p.get("gi_specificity_enabled", True)):
            composite = xp.where(specificity >= xp.float32(specificity_floor), composite, xp.float32(0.0))
        composite = xp.where(territory_x, composite, xp.float32(0.0))
        composite_cpu = _asnumpy(composite).astype(np.float32, copy=False)
        if backend == "cupy":
            del gi_stack, sorted_gi, top_k_mean, gi_max, gi_mean, gi_median, specificity, composite
            for gi in gi_stack_list:
                del gi
            _cupy.get_default_memory_pool().free_all_blocks()
        return composite_cpu, per_channel_gi, backend
    except Exception:
        if backend == "cupy":
            global _CUPY_HOTSPOT_OK, _CUPY_HOTSPOT_WARNED
            _CUPY_HOTSPOT_OK = False
            _CUPY_HOTSPOT_WARNED = True
            cpu_params = dict(p)
            cpu_params["use_gpu"] = False
            return build_multichannel_gi_star(marker_channels, channel_names, base_territory, cpu_params)
        raise


def build_gi_star_hotspot_map(segmentation_signal_map, base_territory, params=None):
    """Legacy single-signal wrapper; delegates to multichannel Gi* scoring."""
    composite, _per_channel, backend = build_multichannel_gi_star(
        [segmentation_signal_map], ["signal"], base_territory, params
    )
    return composite, backend


def build_weak_hotspot_keep(base_territory, nuclei_labels, minimal_keep_labels, gi_star_map, params=None):
    """Retain faint hotspot cytoplasm connected to nucleus/minimal ring."""
    p = _params(params)
    if not bool(p.get("enable_weak_hotspot", True)):
        return np.zeros_like(base_territory, dtype=np.uint32)
    base = np.asarray(base_territory, dtype=np.uint32)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    threshold = float(p.get("weak_hotspot_threshold", 0.8) or 0.8)
    candidate = (np.asarray(gi_star_map, dtype=np.float32) >= threshold) & (base > 0) & (nuclei == 0)
    labels = np.where(candidate, base, 0).astype(np.uint32, copy=False)
    labels = _keep_connected_to_seed(labels, nuclei, minimal_keep_labels, base)
    min_area = int(p.get("weak_min_component_area", 8) or 8)
    if min_area > 1:
        labels = _drop_small_per_cell(labels, min_area)
    return labels


def _keep_connected_to_seed(labels, nuclei, seed_labels, base_territory):
    labels = np.asarray(labels, dtype=np.uint32).copy()
    nuclei = np.asarray(nuclei, dtype=np.uint32)
    seed_labels = np.asarray(seed_labels, dtype=np.uint32)
    n_labels = int(max(labels.max(), nuclei.max(), seed_labels.max()))
    slices = _scipy_ndi.find_objects(base_territory, max_label=n_labels)
    structure = np.ones((3, 3), dtype=bool)
    for lab in range(1, n_labels + 1):
        slc = slices[lab - 1] if lab - 1 < len(slices) else None
        if slc is None:
            continue
        local = labels[slc] == lab
        if not np.any(local):
            continue
        seed = ((nuclei[slc] == lab) | (seed_labels[slc] == lab))
        comps, _n = _scipy_ndi.label(local | seed, structure=structure)
        touching = np.unique(comps[seed])
        touching = touching[touching > 0]
        keep = np.isin(comps, touching) & local if touching.size else np.zeros_like(local, dtype=bool)
        labels[slc][local & ~keep] = 0
    labels[nuclei > 0] = 0
    return labels.astype(np.uint32, copy=False)


def _drop_small_per_cell(labels, min_area):
    out = np.asarray(labels, dtype=np.uint32).copy()
    n_labels = int(out.max())
    structure = np.ones((3, 3), dtype=bool)
    slices = _scipy_ndi.find_objects(out, max_label=n_labels)
    for lab in range(1, n_labels + 1):
        slc = slices[lab - 1] if lab - 1 < len(slices) else None
        if slc is None:
            continue
        local = out[slc] == lab
        comps, n_comp = _scipy_ndi.label(local, structure=structure)
        if n_comp == 0:
            continue
        sizes = np.bincount(comps.ravel())
        keep = sizes >= int(min_area)
        keep[0] = False
        out[slc][local & ~keep[comps]] = 0
    return out


def compose_cds_retained_mask(
    nuclei_labels,
    minimal_keep_labels,
    strong_keep_labels,
    weak_keep_labels,
    base_territory,
    support_map,
    gi_star_map,
    dominant,
    params=None,
):
    """Compose final extent from retained cytoplasm; base territory is only an upper bound."""
    p = _params(params)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    base = np.asarray(base_territory, dtype=np.uint32)
    minimal = np.asarray(minimal_keep_labels, dtype=np.uint32)
    strong = np.asarray(strong_keep_labels, dtype=np.uint32)
    weak = np.asarray(weak_keep_labels, dtype=np.uint32)
    final = np.zeros_like(nuclei, dtype=np.uint32)
    final[nuclei > 0] = nuclei[nuclei > 0]
    final[(minimal > 0) & (base > 0)] = minimal[(minimal > 0) & (base > 0)]
    final[(strong > 0) & (base > 0)] = strong[(strong > 0) & (base > 0)]
    final[(weak > 0) & (base > 0)] = weak[(weak > 0) & (base > 0)]
    final[nuclei > 0] = nuclei[nuclei > 0]
    final, disconnected = _connectivity_cleanup(final, nuclei, int(nuclei.max()))
    guarded, guard_info = _apply_area_guard(final, nuclei, minimal, strong, weak, support_map, gi_star_map, dominant, p)
    guard_info["disconnected_pixels_removed"] = int(disconnected)
    return guarded, guard_info


def _apply_area_guard(final, nuclei, minimal, strong, weak, support_map, gi_star_map, dominant, params):
    out = np.asarray(final, dtype=np.uint32).copy()
    nuclei = np.asarray(nuclei, dtype=np.uint32)
    support = np.asarray(support_map, dtype=np.float32)
    gi = np.asarray(gi_star_map, dtype=np.float32)
    n_labels = int(nuclei.max())
    info = {
        "unsupported_outer_area_removed": {},
        "oversegmentation_guard_applied": {},
    }
    max_added_frac = float(params.get("max_added_area_fraction", 3.0) or 3.0)
    slices = _scipy_ndi.find_objects(out, max_label=n_labels)
    for lab in range(1, n_labels + 1):
        nuc_area = int(np.count_nonzero(nuclei == lab))
        if nuc_area <= 0:
            continue
        cell = out == lab
        final_area = int(np.count_nonzero(cell))
        dom = dominant.get(lab, "")
        ratio_limit = _ratio_limit_for_channel(dom, params)
        max_area = int(max(nuc_area + max_added_frac * nuc_area, ratio_limit * nuc_area))
        # Lymphocyte limit should stay strict even when max_added_area_fraction is higher.
        if _cell_type_hint(dom) == "lymphocyte":
            max_area = int(ratio_limit * nuc_area)
        if final_area <= max_area:
            info["unsupported_outer_area_removed"][lab] = 0
            info["oversegmentation_guard_applied"][lab] = False
            continue
        slc0 = slices[lab - 1] if lab - 1 < len(slices) else None
        if slc0 is None:
            continue
        slc = _expand_slice(slc0, out.shape, 2)
        local_cell = out[slc] == lab
        local_nuc = nuclei[slc] == lab
        keep = local_nuc | (minimal[slc] == lab)
        remaining = local_cell & ~keep
        available = int(max(max_area - np.count_nonzero(keep), 0))
        if available > 0 and np.any(remaining):
            evidence = support[slc] + np.clip(gi[slc] / max(float(params.get("weak_hotspot_threshold", 0.8) or 0.8), 1.0e-6), 0.0, 2.0)
            coords = np.flatnonzero(remaining)
            order = np.argsort(evidence.ravel()[coords])[::-1]
            chosen = coords[order[:available]]
            flat_keep = keep.ravel()
            flat_keep[chosen] = True
            keep = flat_keep.reshape(keep.shape)
        removed = local_cell & ~keep
        local_out = out[slc]
        local_out[removed] = 0
        info["unsupported_outer_area_removed"][lab] = int(np.count_nonzero(removed))
        info["oversegmentation_guard_applied"][lab] = True
    out[nuclei > 0] = nuclei[nuclei > 0]
    return out.astype(np.uint32, copy=False), info


def _interface_band(base_territory, radius=2):
    labels = np.asarray(base_territory, dtype=np.uint32)
    maxf = _scipy_ndi.maximum_filter(labels, size=3)
    minf = _scipy_ndi.minimum_filter(labels, size=3)
    interface = (labels > 0) & (maxf != minf)
    if radius > 0:
        interface = _scipy_ndi.binary_dilation(interface, iterations=int(radius))
    return interface & (labels > 0)


def run_cds_boundary_adjustment(base_territory, nuclei_labels, dist_from_nuc, support_map, weak_support_map, params=None):
    """Adjust only interface ownership; territory existence is unchanged."""
    p = _params(params)
    from skimage.segmentation import watershed

    base = np.asarray(base_territory, dtype=np.uint32)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    support = np.asarray(support_map, dtype=np.float32)
    weak = np.asarray(weak_support_map, dtype=np.float32)
    dist = np.asarray(dist_from_nuc, dtype=np.float32)
    gradient = _scipy_ndi.gaussian_gradient_magnitude(support + weak, sigma=1.0).astype(np.float32, copy=False)
    energy = (
        float(p.get("distance_penalty_weight", 0.03) or 0.0) * dist
        - float(p.get("support_weight", 1.0) or 0.0) * support
        - float(p.get("weak_support_weight", 0.5) or 0.0) * weak
        + float(p.get("boundary_gradient_weight", 0.25) or 0.0) * gradient
    ).astype(np.float32, copy=False)

    adjusted_all = watershed(energy, markers=nuclei, mask=base > 0).astype(np.uint32, copy=False)
    band = _interface_band(base, radius=max(1, int(p.get("weak_hotspot_connectivity", 8) or 8) // 4))
    adjusted = np.zeros_like(base, dtype=np.uint32)
    adjusted[(base > 0) & ~band] = base[(base > 0) & ~band]
    adjusted[band] = adjusted_all[band]
    adjusted[nuclei > 0] = nuclei[nuclei > 0]
    return adjusted, energy, band


def _connectivity_cleanup(labels, nuclei, n_labels):
    final = np.asarray(labels, dtype=np.uint32).copy()
    object_slices = _scipy_ndi.find_objects(final, max_label=n_labels)
    removed = 0
    for lab in range(1, n_labels + 1):
        slc = object_slices[lab - 1] if lab - 1 < len(object_slices) else None
        if slc is None:
            continue
        local = final[slc] == lab
        comps, _ = _scipy_ndi.label(local, structure=np.ones((3, 3), dtype=bool))
        touching = np.unique(comps[(nuclei[slc] == lab) & local])
        touching = touching[touching > 0]
        keep = np.isin(comps, touching) if touching.size else np.zeros_like(local, dtype=bool)
        drop = local & ~keep
        removed += int(np.count_nonzero(drop))
        final[slc][drop] = 0
    final[nuclei > 0] = nuclei[nuclei > 0]
    return final, removed


def apply_cds_shrink_and_smooth(adjusted_labels, nuclei_labels, base_territory, params=None):
    """Conservative shrink/smooth while preserving nuclei and preventing collapse."""
    p = _params(params)
    labels = np.asarray(adjusted_labels, dtype=np.uint32)
    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    base = np.asarray(base_territory, dtype=np.uint32)
    out = np.zeros_like(labels, dtype=np.uint32)
    n_labels = int(max(labels.max(), nuclei.max()))
    shrink = max(0, int(round(float(p.get("shrink_pixels", p.get("nucleus_shrink", 3)) or 0))))
    fallback_expand = max(2, min(4, shrink if shrink > 0 else 2))
    fallback = _expand_labels(nuclei, fallback_expand).astype(np.uint32, copy=False)
    object_slices = _scipy_ndi.find_objects(labels, max_label=n_labels)
    prevented = 0

    for lab in range(1, n_labels + 1):
        nuc_mask_full = nuclei == lab
        if not np.any(nuc_mask_full):
            continue
        slc0 = object_slices[lab - 1] if lab - 1 < len(object_slices) else None
        if slc0 is None:
            slc0 = _scipy_ndi.find_objects(nuc_mask_full.astype(np.uint8), max_label=1)[0]
        slc = _expand_slice(slc0, labels.shape, shrink + 3)
        local = labels[slc] == lab
        nuc_local = nuclei[slc] == lab
        if shrink > 0 and np.count_nonzero(local) > np.count_nonzero(nuc_local):
            work = _scipy_ndi.binary_erosion(local, iterations=shrink)
            work = _scipy_ndi.binary_opening(work, structure=np.ones((3, 3), dtype=bool))
            work = _scipy_ndi.binary_closing(work, structure=np.ones((3, 3), dtype=bool))
            work |= nuc_local
        else:
            work = local | nuc_local
        if np.count_nonzero(work & ~nuc_local) == 0:
            fb = (fallback[slc] == lab) & ((base[slc] == lab) | nuc_local)
            work = fb | nuc_local
            prevented += 1
        local_out = out[slc]
        local_out[work] = lab

    out[nuclei > 0] = nuclei[nuclei > 0]
    out, disconnected_removed = _connectivity_cleanup(out, nuclei, n_labels)
    return out.astype(np.uint32, copy=False), int(prevented), int(disconnected_removed)


def _main_channel_for_cell(mask, marker_channels, channel_names, channel_thresholds):
    best_name = ""
    best_score = -1.0
    best_mean = 0.0
    best_low = 0.0
    for name, arr in zip(channel_names, marker_channels):
        vals = np.asarray(arr, dtype=np.float32)[mask]
        mean_val = float(np.mean(vals)) if vals.size else 0.0
        low = float(channel_thresholds.get(name, {}).get("auto_low", 0.0) or 0.0)
        score = mean_val / max(low, 1.0e-6)
        if score > best_score:
            best_name, best_score, best_mean, best_low = name, score, mean_val, low
    return best_name, best_low, best_mean


def _qc_rows(
    nuclei,
    base,
    minimal,
    strong,
    weak_hotspot,
    final,
    marker_channels,
    channel_names,
    thresholds,
    support_map,
    gi_star_map,
    dominant,
    guard_info,
):
    n_labels = int(nuclei.max())
    nucleus_area = np.bincount(nuclei.ravel(), minlength=n_labels + 1)
    base_area = np.bincount(base.ravel(), minlength=n_labels + 1)
    minimal_area = np.bincount(minimal.ravel(), minlength=n_labels + 1)
    strong_area = np.bincount(strong.ravel(), minlength=n_labels + 1)
    weak_hotspot_area = np.bincount(weak_hotspot.ravel(), minlength=n_labels + 1)
    final_area_bins = np.bincount(final.ravel(), minlength=n_labels + 1)
    rows = []
    prevented_count = 0
    weak_count = 0
    for lab in range(1, n_labels + 1):
        nuc_area = int(nucleus_area[lab]) if lab < len(nucleus_area) else 0
        init_area = int(base_area[lab]) if lab < len(base_area) else 0
        final_area = int(final_area_bins[lab]) if lab < len(final_area_bins) else 0
        cell = final == lab
        added = cell & (nuclei != lab)
        added_area = int(np.count_nonzero(added))
        gi_vals = gi_star_map[cell]
        gi_score_mean = float(np.mean(gi_vals)) if gi_vals.size else 0.0
        gi_score_p90 = float(np.percentile(gi_vals, 90)) if gi_vals.size else 0.0
        weak_support_mean = max(0.0, gi_score_mean / 3.0)
        main_channel, local_threshold, mean_signal = _main_channel_for_cell(
            added if np.any(added) else cell, marker_channels, channel_names, thresholds
        )
        if not main_channel:
            main_channel = dominant.get(lab, "")
        dominant_channel = dominant.get(lab, main_channel)
        type_hint = _cell_type_hint(dominant_channel)
        marker_support_score = 0.0
        if local_threshold > 0:
            marker_support_score = float(np.clip((mean_signal - local_threshold) / local_threshold, 0.0, 1.0))
        elif mean_signal > 0:
            marker_support_score = 1.0
        strong_keep_area = int(strong_area[lab]) if lab < len(strong_area) else 0
        weak_keep_area = int(weak_hotspot_area[lab]) if lab < len(weak_hotspot_area) else 0
        if strong_keep_area > 0 and marker_support_score >= 0.20:
            signal_mode = "strong_signal"
            weak_flag = False
            low_conf = False
            reason = ""
        elif weak_keep_area > 0:
            signal_mode = "weak_hotspot"
            weak_flag = True
            low_conf = False
            reason = ""
            weak_count += 1
        else:
            signal_mode = "minimal_geometry_prior"
            weak_flag = True
            low_conf = True
            reason = "minimal_geometry_prior"
            weak_count += 1
        prevented = bool(added_area > 0 and final_area > nuc_area and marker_support_score < 0.05)
        prevented_count += int(prevented)
        removed = int((guard_info.get("unsupported_outer_area_removed") or {}).get(lab, 0))
        guard_applied = bool((guard_info.get("oversegmentation_guard_applied") or {}).get(lab, False))
        min_area = int(minimal_area[lab]) if lab < len(minimal_area) else 0
        rows.append({
            "cell_id": lab,
            "nucleus_area": nuc_area,
            "initial_hq_area": init_area,
            "refined_area": final_area,
            "added_area": added_area,
            "added_area_fraction": float(added_area) / max(float(init_area), 1.0),
            "cell_to_nucleus_ratio": float(final_area) / max(float(nuc_area), 1.0),
            "main_channel": main_channel,
            "local_threshold": float(local_threshold),
            "mean_refine_signal": float(mean_signal),
            "conflict_pixel_count": 0,
            "refinement_applied": bool(added_area > 0),
            "fallback_to_hq": False,
            "low_confidence_flag": bool(low_conf),
            "low_confidence_reason": reason,
            "marker_support_score": marker_support_score,
            "signal_mode": signal_mode,
            "weak_signal_flag": bool(weak_flag),
            "prevented_nucleus_only_collapse": bool(prevented),
            "weak_support_mean": weak_support_mean,
            "support_confidence": float(np.clip(marker_support_score + weak_support_mean, 0.0, 1.0)),
            "base_territory_area": init_area,
            "minimal_keep_area": min_area,
            "strong_keep_area": strong_keep_area,
            "weak_hotspot_keep_area": weak_keep_area,
            "final_area": final_area,
            "dominant_channel": dominant_channel,
            "dominant_cell_type_hint": type_hint,
            "gi_score_mean": gi_score_mean,
            "gi_score_p90": gi_score_p90,
            "strong_support_area": strong_keep_area,
            "weak_hotspot_area": weak_keep_area,
            "unsupported_outer_area_removed": removed,
            "oversegmentation_guard_applied": guard_applied,
        })
    return rows, prevented_count, weak_count


def run_constrained_donut_segmentation(
    nuclei_labels,
    marker_channels,
    channel_names,
    params=None,
    logger=None,
    return_layers=True,
    progress_callback=None,
    cancel_check=None,
):
    """Run CDS and return a dict compatible with run_hq2_segmentation()."""
    started = time.perf_counter()
    p = _params(params)
    timings = {}
    warnings = []
    backend_name = "cupy_available" if (_HAS_CUPY and p.get("use_gpu", True)) else "numpy"

    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
    marker_channels = [np.asarray(arr) for arr in (marker_channels or [])]
    channel_names = list(channel_names or [])[:len(marker_channels)]
    if not channel_names:
        channel_names = [f"channel_{idx + 1}" for idx in range(len(marker_channels))]

    if n_labels == 0:
        empty = np.zeros_like(nuclei, dtype=np.uint32)
        result = {
            "final_labels": empty,
            "nuclei_labels": nuclei.copy(),
            "qc_rows": [],
            "stats": {"cells_refined": 0, "cells_fallback": 0, "added_pixels_total": 0},
            "metadata": {"timings": {}, "csd_backend": backend_name, "channel_thresholds": {}},
        }
        if return_layers:
            result.update({
                "hq_proposal_labels": empty.copy(),
                "imagej_proposal_labels": empty.copy(),
                "high_confidence_core_labels": empty.copy(),
                "expansion_added_pixels": empty.copy(),
                "refinement_added_pixels": empty.copy(),
                "base_territory_mask": empty.copy(),
                "minimal_keep_mask": empty.copy(),
                "strong_keep_mask": empty.copy(),
                "weak_hotspot_keep_mask": empty.copy(),
                "gi_star_map": empty.astype(np.float32),
                "per_channel_gi_maps": {},
                "support_map": empty.astype(np.float32),
                "weak_support_map": empty.astype(np.float32),
                "shrinked_mask": empty.copy(),
                "signal_map": empty.astype(np.float32),
            })
        return result

    try:
        _check_safety(p, started, cancel_check, "CDS-build-territory")
        t0 = time.perf_counter()
        base_territory, dist_from_nuc = build_nucleus_owned_territory(nuclei, p)
        timings["build_territory_seconds"] = time.perf_counter() - t0
        _log(logger, progress_callback, f"[CDS] build territory seconds={timings['build_territory_seconds']:.3f}")

        _check_safety(p, started, cancel_check, "CDS-support-maps")
        t0 = time.perf_counter()
        signal_map, support_map, per_channel_norm_maps, dominant, channel_thresholds = build_soft_constraint_maps(
            nuclei, base_territory, marker_channels, channel_names, p
        )
        timings["support_maps_seconds"] = time.perf_counter() - t0
        _log(logger, progress_callback, f"[CDS] support maps seconds={timings['support_maps_seconds']:.3f}")

        _check_safety(p, started, cancel_check, "CDS-retained-cytoplasm")
        t0 = time.perf_counter()
        minimal_keep = _expand_labels(nuclei, float(p.get("minimal_radius", 3) or 3)).astype(np.uint32, copy=False)
        minimal_keep[base_territory == 0] = 0
        minimal_keep[nuclei > 0] = nuclei[nuclei > 0]
        strong_keep = build_strong_signal_keep(base_territory, support_map, nuclei, minimal_keep, p)
        timings["strong_keep_seconds"] = time.perf_counter() - t0
        _log(logger, progress_callback, f"[CDS] strong keep seconds={timings['strong_keep_seconds']:.3f}")

        _check_safety(p, started, cancel_check, "CDS-weak-hotspot")
        t0 = time.perf_counter()
        gi_star_map, per_channel_gi, gi_backend = build_multichannel_gi_star(
            marker_channels, channel_names, base_territory, p
        )
        weak_hotspot_keep = build_weak_hotspot_keep(base_territory, nuclei, minimal_keep, gi_star_map, p)
        weak_support_map = np.clip(gi_star_map / max(float(p.get("weak_hotspot_threshold", 0.8) or 0.8), 1.0e-6), 0.0, 1.0)
        timings["weak_hotspot_seconds"] = time.perf_counter() - t0
        _log(logger, progress_callback, f"[CDS] weak hotspot seconds={timings['weak_hotspot_seconds']:.3f}")

        _check_safety(p, started, cancel_check, "CDS-compose")
        t0 = time.perf_counter()
        retained, guard_info = compose_cds_retained_mask(
            nuclei, minimal_keep, strong_keep, weak_hotspot_keep, base_territory,
            support_map, gi_star_map, dominant, p
        )
        timings["compose_seconds"] = time.perf_counter() - t0
        boundary_energy = gi_star_map.astype(np.float32, copy=False)
        boundary_band = _interface_band(base_territory, radius=1)
        adjusted = retained
        _log(logger, progress_callback, f"[CDS] compose seconds={timings['compose_seconds']:.3f}")

        _check_safety(p, started, cancel_check, "CDS-shrink")
        t0 = time.perf_counter()
        final, prevented_by_shrink, shrink_removed = apply_cds_shrink_and_smooth(retained, nuclei, base_territory, p)
        disconnected_removed = int(shrink_removed) + int(guard_info.get("disconnected_pixels_removed", 0))
        timings["shrink_seconds"] = time.perf_counter() - t0
        _log(logger, progress_callback, f"[CDS] shrink seconds={timings['shrink_seconds']:.3f}")

        qc_rows, prevented_qc, weak_signal_cells = _qc_rows(
            nuclei, base_territory, minimal_keep, strong_keep, weak_hotspot_keep, final,
            marker_channels, channel_names, channel_thresholds, support_map, gi_star_map,
            dominant, guard_info
        )
        prevented_nucleus_only = int(max(prevented_by_shrink, prevented_qc))
        fallback_count = 0
        conflict_total = int(np.count_nonzero(boundary_band))
        watershed_backend = "retained_mask_no_extent_watershed"
    except CSDFallback as exc:
        warnings.append(str(exc))
        base_territory = nuclei.copy()
        adjusted = nuclei.copy()
        final = nuclei.copy()
        signal_map = np.zeros_like(nuclei, dtype=np.float32)
        support_map = np.zeros_like(nuclei, dtype=np.float32)
        weak_support_map = np.zeros_like(nuclei, dtype=np.float32)
        gi_star_map = np.zeros_like(nuclei, dtype=np.float32)
        per_channel_gi = {}
        minimal_keep = nuclei.copy()
        strong_keep = np.zeros_like(nuclei, dtype=np.uint32)
        weak_hotspot_keep = np.zeros_like(nuclei, dtype=np.uint32)
        boundary_energy = np.zeros_like(nuclei, dtype=np.float32)
        boundary_band = np.zeros_like(nuclei, dtype=bool)
        channel_thresholds = {}
        dominant = {}
        qc_rows = []
        prevented_nucleus_only = 0
        weak_signal_cells = 0
        fallback_count = n_labels
        disconnected_removed = 0
        conflict_total = 0
        gi_backend = "not_run"
        watershed_backend = "fallback_not_run"
        _log(logger, progress_callback, f"[CDS] fallback to nuclei only: {exc}")

    final[nuclei > 0] = nuclei[nuclei > 0]
    expansion_added = np.where((final > 0) & (nuclei == 0), final, 0).astype(np.uint32, copy=False)
    timings["total_seconds"] = time.perf_counter() - started
    added_pixels_total = int(np.count_nonzero(expansion_added))
    stats = {
        "cells_refined": int(sum(1 for row in qc_rows if row.get("refinement_applied"))),
        "cells_fallback": int(fallback_count),
        "added_pixels_total": added_pixels_total,
        "disconnected_pixels_removed": int(disconnected_removed),
        "conflict_pixels_total": int(conflict_total),
        "prevented_nucleus_only_collapse": int(prevented_nucleus_only),
        "weak_signal_cells": int(weak_signal_cells),
        "unsupported_outer_area_removed": int(sum(row.get("unsupported_outer_area_removed", 0) for row in qc_rows)),
    }
    metadata = {
        "timings": timings,
        "fallback": bool(warnings),
        "fallback_reason": "; ".join(warnings),
        "warnings": warnings,
        "peak_memory_estimate_mb": _rss_mb(),
        "csd_backend": backend_name,
        "watershed_backend": watershed_backend,
        "csd_mode": "retained_cytoplasm_constrained_donut",
        "hq2_mode": "retained_cytoplasm_constrained_donut",
        "signal_map_mode": "raw_clipped_signal_for_retention",
        "gating_role": "qc_annotation_only",
        "segmentation_retention_thresholds": {
            "strong_support_threshold": p.get("strong_support_threshold", 0.12),
            "weak_hotspot_threshold": p.get("weak_hotspot_threshold", 0.8),
        },
        "qc_gating_threshold": {
            "bg_sigma_factor": p.get("bg_sigma_factor", 3.0),
            "saturation_percentile": p.get("saturation_percentile", 99.8),
        },
        "imagej_proposal_enabled": False,
        "channel_thresholds": channel_thresholds,
        "dominant_channel_per_cell": {str(k): v for k, v in dominant.items()},
        "minimal_radius": p.get("minimal_radius", 3),
        "default_lymphocyte_radius": p.get("default_lymphocyte_radius", 6),
        "donut_size": p.get("donut_size", 18),
        "max_cell_radius": p.get("max_cell_radius", p.get("donut_size", 18)),
        "shrink_pixels": p.get("shrink_pixels", p.get("nucleus_shrink", 2)),
        "strong_support_threshold": p.get("strong_support_threshold", 0.12),
        "weak_hotspot_threshold": p.get("weak_hotspot_threshold", 0.8),
        "gi_mode": "multichannel_specificity_weighted",
        "gi_kernel_size": p.get("gi_kernel_size", 7),
        "gi_background_kernel_size": p.get("gi_background_kernel_size", 31),
        "gi_background_mode": p.get("gi_background_mode", "local"),
        "gi_backend": gi_backend,
        "gi_top_k": int(_gi_top_k_count(len(marker_channels), p)) if marker_channels else 0,
        "gi_specificity_enabled": bool(p.get("gi_specificity_enabled", True)),
        "gi_specificity_floor": float(p.get("gi_specificity_floor", 0.05) or 0.0),
        "max_added_area_fraction": p.get("max_added_area_fraction", 3.0),
        "max_cell_to_nucleus_ratio": p.get("max_cell_to_nucleus_ratio", 8.0),
        "lymphocyte_max_cell_to_nucleus_ratio": p.get("lymphocyte_max_cell_to_nucleus_ratio", 4.0),
        "large_cell_max_cell_to_nucleus_ratio": p.get("large_cell_max_cell_to_nucleus_ratio", 12.0),
        "enable_weak_hotspot": bool(p.get("enable_weak_hotspot", True)),
        "enable_strong_signal_keep": bool(p.get("enable_strong_signal_keep", True)),
        "support_weight": p.get("support_weight", 1.0),
        "weak_support_weight": p.get("weak_support_weight", 0.5),
        "distance_penalty_weight": p.get("distance_penalty_weight", 0.03),
        "neighbor_penalty_weight": p.get("neighbor_penalty_weight", 0.0),
        "boundary_gradient_weight": p.get("boundary_gradient_weight", 0.25),
        "constraint_low_percentile": p.get("constraint_low_percentile", 0.5),
        "constraint_high_percentile": p.get("constraint_high_percentile", 99.5),
        "constraint_mode": p.get("constraint_mode", "per_cell_best_channel"),
        "local_contrast_enabled": bool(p.get("local_contrast_enabled", False)),
        "local_contrast_radius": p.get("local_contrast_radius", 15),
        "weak_local_z_threshold": p.get("weak_local_z_threshold", 0.75),
        "weak_min_component_area": p.get("weak_min_component_area", 8),
        "weak_hotspot_connectivity": p.get("weak_hotspot_connectivity", 8),
        "prevented_nucleus_only_collapse": int(prevented_nucleus_only),
        "weak_signal_cells": int(weak_signal_cells),
        "fallback_count": int(fallback_count),
        "refinement_added_pixels": added_pixels_total,
    }
    _log(logger, progress_callback, f"[CDS] prevented nucleus-only collapse={prevented_nucleus_only}")
    _log(logger, progress_callback, f"[CDS] weak signal cells={weak_signal_cells}")
    _log(logger, progress_callback, f"[CDS] final labels count={int(final.max())}")

    result = {
        "final_labels": final.astype(np.uint32, copy=False),
        "nuclei_labels": nuclei.astype(np.uint32, copy=False),
        "qc_rows": qc_rows,
        "stats": stats,
        "metadata": metadata,
    }
    if return_layers:
        zeros = np.zeros_like(nuclei, dtype=np.uint32)
        result.update({
            "hq_proposal_labels": base_territory.astype(np.uint32, copy=False),
            "imagej_proposal_labels": zeros,
            "high_confidence_core_labels": adjusted.astype(np.uint32, copy=False),
            "expansion_added_pixels": expansion_added,
            "refinement_added_pixels": expansion_added.copy(),
            "base_territory_mask": base_territory.astype(np.uint32, copy=False),
            "minimal_keep_mask": minimal_keep.astype(np.uint32, copy=False),
            "strong_keep_mask": strong_keep.astype(np.uint32, copy=False),
            "weak_hotspot_keep_mask": weak_hotspot_keep.astype(np.uint32, copy=False),
            "gi_star_map": gi_star_map.astype(np.float32, copy=False),
            "support_map": support_map.astype(np.float32, copy=False),
            "weak_support_map": weak_support_map.astype(np.float32, copy=False),
            "shrinked_mask": final.astype(np.uint32, copy=False),
            "signal_map": signal_map.astype(np.float32, copy=False),
            "boundary_energy": boundary_energy.astype(np.float32, copy=False),
            "per_channel_gi_maps": {
                name: arr.astype(np.float32, copy=False)
                for name, arr in per_channel_gi.items()
            },
        })
    return result


def write_csd_qc_table(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSD_QC_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows or []:
            writer.writerow({k: row.get(k, "") for k in CSD_QC_FIELDS})


def csd_metadata_fields(params, paths):
    runtime = dict(params.get("csd_runtime_metadata") or params.get("hq2_runtime_metadata") or {})
    return {
        "method": "cellpose_nuclei_csd",
        "display_name": "Cellpose nuclei + CDS",
        "hq2_mode": "retained_cytoplasm_constrained_donut",
        "csd_mode": "retained_cytoplasm_constrained_donut",
        "gating_role": "qc_annotation_only",
        "imagej_proposal_enabled": False,
        "nuclei_method": "cellpose_nuclei_dapi",
        "hq_channels": parse_hq_channels(params.get("hq_channels") or []),
        "hq_input_mode": params.get("hq_input_mode", "selected_channels_from_source"),
        "csd_parameters": {
            "minimal_radius": params.get("minimal_radius", 3),
            "default_lymphocyte_radius": params.get("default_lymphocyte_radius", 6),
            "donut_size": params.get("donut_size", 18),
            "max_cell_radius": params.get("max_cell_radius", params.get("donut_size", 18)),
            "shrink_pixels": params.get("shrink_pixels", params.get("nucleus_shrink", 2)),
            "strong_support_threshold": params.get("strong_support_threshold", 0.12),
            "weak_hotspot_threshold": params.get("weak_hotspot_threshold", 0.8),
            "gi_kernel_size": params.get("gi_kernel_size", 7),
            "gi_background_kernel_size": params.get("gi_background_kernel_size", 31),
            "gi_background_mode": params.get("gi_background_mode", "local"),
            "gi_specificity_enabled": params.get("gi_specificity_enabled", True),
            "gi_top_k": params.get("gi_top_k", 0),
            "gi_specificity_floor": params.get("gi_specificity_floor", 0.05),
            "max_added_area_fraction": params.get("max_added_area_fraction", 3.0),
            "max_cell_to_nucleus_ratio": params.get("max_cell_to_nucleus_ratio", 8.0),
            "lymphocyte_max_cell_to_nucleus_ratio": params.get("lymphocyte_max_cell_to_nucleus_ratio", 4.0),
            "large_cell_max_cell_to_nucleus_ratio": params.get("large_cell_max_cell_to_nucleus_ratio", 12.0),
            "enable_weak_hotspot": params.get("enable_weak_hotspot", True),
            "enable_strong_signal_keep": params.get("enable_strong_signal_keep", True),
            "support_weight": params.get("support_weight", 1.0),
            "weak_support_weight": params.get("weak_support_weight", 0.5),
            "distance_penalty_weight": params.get("distance_penalty_weight", 0.03),
            "neighbor_penalty_weight": params.get("neighbor_penalty_weight", 0.0),
            "boundary_gradient_weight": params.get("boundary_gradient_weight", 0.25),
            "constraint_low_percentile": params.get("constraint_low_percentile", 0.5),
            "constraint_high_percentile": params.get("constraint_high_percentile", 99.5),
            "constraint_mode": params.get("constraint_mode", "per_cell_best_channel"),
            "local_contrast_enabled": params.get("local_contrast_enabled", False),
            "local_contrast_radius": params.get("local_contrast_radius", 15),
            "weak_local_z_threshold": params.get("weak_local_z_threshold", 0.75),
            "weak_min_component_area": params.get("weak_min_component_area", 8),
            "weak_hotspot_connectivity": params.get("weak_hotspot_connectivity", 8),
            "timeout_seconds": params.get("timeout_seconds", 600),
            "memory_guard_mb": params.get("memory_guard_mb", 0),
        },
        "channel_thresholds": runtime.get("channel_thresholds", {}),
        "dominant_channel_per_cell": runtime.get("dominant_channel_per_cell", {}),
        "signal_map_mode": runtime.get("signal_map_mode", "raw_clipped_signal_for_retention"),
        "gi_mode": runtime.get("gi_mode", "multichannel_specificity_weighted"),
        "gi_backend": runtime.get("gi_backend", ""),
        "gi_top_k": int(runtime.get("gi_top_k", params.get("gi_top_k", 0)) or 0),
        "gi_specificity_enabled": bool(runtime.get("gi_specificity_enabled", params.get("gi_specificity_enabled", True))),
        "gi_specificity_floor": float(runtime.get("gi_specificity_floor", params.get("gi_specificity_floor", 0.05)) or 0.0),
        "segmentation_retention_thresholds": runtime.get("segmentation_retention_thresholds", {}),
        "qc_gating_threshold": runtime.get("qc_gating_threshold", {}),
        "prevented_nucleus_only_collapse": int(runtime.get("prevented_nucleus_only_collapse", 0) or 0),
        "weak_signal_cells": int(runtime.get("weak_signal_cells", 0) or 0),
        "fallback_count": int(runtime.get("fallback_count", 0) or 0),
        "runtime_by_stage": runtime.get("timings", {}),
        "nuclei_mask_path": paths.get("nuclei_mask_path", ""),
        "initial_hq_mask_path": paths.get("initial_hq_mask_path") or paths.get("hq_proposal_mask_path", ""),
        "hq_proposal_mask_path": paths.get("hq_proposal_mask_path", ""),
        "imagej_proposal_mask_path": paths.get("imagej_proposal_mask_path", ""),
        "core_mask_path": paths.get("core_mask_path", ""),
        "expansion_mask_path": paths.get("expansion_mask_path", ""),
        "refinement_added_pixels_path": paths.get("refinement_added_pixels_path") or paths.get("expansion_mask_path", ""),
        "final_cell_mask_path": paths.get("final_cell_mask_path", ""),
        "base_territory_mask_path": paths.get("base_territory_mask_path", ""),
        "minimal_keep_mask_path": paths.get("minimal_keep_mask_path", ""),
        "strong_keep_mask_path": paths.get("strong_keep_mask_path", ""),
        "weak_hotspot_keep_mask_path": paths.get("weak_hotspot_keep_mask_path", ""),
        "gi_star_map_path": paths.get("gi_star_map_path", ""),
        "support_map_path": paths.get("support_map_path", ""),
        "weak_support_map_path": paths.get("weak_support_map_path", ""),
        "shrinked_mask_path": paths.get("shrinked_mask_path", ""),
        "qc_table_path": paths.get("qc_table_path", ""),
        "csd_runtime_metadata": runtime,
        "hq2_runtime_metadata": runtime,
    }


def save_csd_outputs(output_dir, prefix, result, params):
    os.makedirs(output_dir, exist_ok=True)
    paths = {}
    mask_mapping = {
        "nuclei_mask_path": "nuclei_labels",
        "hq_proposal_mask_path": "hq_proposal_labels",
        "initial_hq_mask_path": "hq_proposal_labels",
        "imagej_proposal_mask_path": "imagej_proposal_labels",
        "core_mask_path": "high_confidence_core_labels",
        "expansion_mask_path": "expansion_added_pixels",
        "refinement_added_pixels_path": "refinement_added_pixels",
        "final_cell_mask_path": "final_labels",
        "base_territory_mask_path": "base_territory_mask",
        "minimal_keep_mask_path": "minimal_keep_mask",
        "strong_keep_mask_path": "strong_keep_mask",
        "weak_hotspot_keep_mask_path": "weak_hotspot_keep_mask",
        "shrinked_mask_path": "shrinked_mask",
    }
    for path_key, result_key in mask_mapping.items():
        if result_key not in result:
            continue
        path = os.path.join(output_dir, f"{prefix}_{result_key}.npy")
        np.save(path, np.asarray(result[result_key], dtype=np.uint32))
        paths[path_key] = os.path.abspath(path)
    for path_key, result_key in (
        ("support_map_path", "support_map"),
        ("weak_support_map_path", "weak_support_map"),
        ("gi_star_map_path", "gi_star_map"),
    ):
        if result_key in result:
            path = os.path.join(output_dir, f"{prefix}_{result_key}.npy")
            np.save(path, np.asarray(result[result_key], dtype=np.float32))
            paths[path_key] = os.path.abspath(path)
    qc_path = os.path.join(output_dir, f"{prefix}_csd_qc_table.csv")
    write_csd_qc_table(qc_path, result.get("qc_rows") or [])
    paths["qc_table_path"] = os.path.abspath(qc_path)
    meta_params = dict(params or {})
    if result.get("metadata"):
        meta_params["csd_runtime_metadata"] = result.get("metadata")
        meta_params["hq2_runtime_metadata"] = result.get("metadata")
    meta = csd_metadata_fields(meta_params, paths)
    meta_path = os.path.join(output_dir, f"{prefix}_segmentation_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"[CDS] outputs saved to={output_dir}")
    return paths, meta_path
