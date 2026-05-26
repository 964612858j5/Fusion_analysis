"""Constrained Signal Donut (CSD) nucleus-seeded cytoplasm segmentation."""

import csv
import json
import os
import time

import numpy as np

try:
    import cupy as _cupy
    from cupyx.scipy import ndimage as _cupyx_ndi
    _HAS_CUPY = True
except ImportError:
    _cupy = None
    _cupyx_ndi = None
    _HAS_CUPY = False

try:
    from cucim.skimage.segmentation import watershed as _cucim_watershed
    _HAS_CUCIM_WATERSHED = True
except ImportError:
    _cucim_watershed = None
    _HAS_CUCIM_WATERSHED = False

from scipy import ndimage as _scipy_ndi

from .hq2_marker_segmentation import HQ2_QC_FIELDS, _expand_labels
from .hq_marker_segmentation import parse_hq_channels


DEFAULT_CSD_PARAMS = {
    "donut_size": 40,
    "nucleus_shrink": 3,
    "bg_sigma_factor": 3.0,
    "saturation_percentile": 99.8,
    "max_circularity": 0.92,
    "circularity_ratio_threshold": 3.0,
    "circularity_fallback_expand": 3,
    "use_gpu": True,
    "timeout_seconds": 600,
    "memory_guard_mb": 0,
}


class CSDFallback(RuntimeError):
    """Raised when CSD must stop because of cancellation or safety limits."""


def _params(params):
    out = dict(DEFAULT_CSD_PARAMS)
    out.update(dict(params or {}))
    return out


def _use_gpu(params):
    requested = str(params.get("use_gpu", True)).strip().lower() not in {"0", "false", "no", "off", "cpu"}
    return bool(requested and _HAS_CUPY)


def _xp_backend(params):
    if _use_gpu(params):
        return _cupy, _cupyx_ndi, "cupy"
    return np, _scipy_ndi, "numpy"


def _asnumpy(arr):
    if _HAS_CUPY and isinstance(arr, _cupy.ndarray):
        return _cupy.asnumpy(arr)
    return np.asarray(arr)


def _watershed_with_gpu_preference(dist_x, nuclei_x, gated_region_x, dist_cpu, nuclei_cpu, gated_region_cpu):
    """Use cuCIM watershed when available, otherwise CPU skimage watershed."""
    from skimage.segmentation import watershed as _skimage_watershed

    if _HAS_CUPY and _HAS_CUCIM_WATERSHED and isinstance(dist_x, _cupy.ndarray):
        try:
            labels_x = _cucim_watershed(dist_x, markers=nuclei_x, mask=gated_region_x)
            return _cupy.asnumpy(labels_x).astype(np.uint32, copy=False), "cucim_gpu"
        except Exception:
            pass
    labels = _skimage_watershed(dist_cpu, markers=nuclei_cpu, mask=gated_region_cpu)
    return np.asarray(labels, dtype=np.uint32), "skimage_cpu"


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


def _check_safety(params, started, cancel_check=None, stage="CSD"):
    if cancel_check is not None and cancel_check():
        raise CSDFallback(f"{stage} cancelled")
    timeout = float(params.get("timeout_seconds", 600) or 0)
    if timeout > 0 and time.perf_counter() - started > timeout:
        raise CSDFallback(f"{stage} exceeded timeout_seconds={timeout:g}")
    guard = float(params.get("memory_guard_mb", 0) or 0)
    rss = _rss_mb()
    if guard > 0 and rss > guard:
        raise CSDFallback(f"{stage} exceeded memory_guard_mb={guard:g} (rss={rss:.1f} MB)")


def _channel_threshold(raw_np, far_bg, bg_sigma_factor, saturation_percentile):
    raw = np.asarray(raw_np)
    finite = np.isfinite(raw)
    bg_vals = raw[far_bg & finite]
    if bg_vals.size == 0:
        bg_vals = raw[(raw > 0) & finite]
    if bg_vals.size == 0:
        bg_mean = 0.0
        bg_std = 0.0
    else:
        bg_mean = float(np.mean(bg_vals))
        bg_std = float(np.std(bg_vals))
    auto_low = bg_mean + float(bg_sigma_factor) * bg_std
    if bg_std == 0.0:
        auto_low = float(np.nextafter(auto_low, np.inf))
    positive = raw[(raw > 0) & finite]
    auto_high = float(np.percentile(positive, float(saturation_percentile))) if positive.size else float("inf")
    if auto_high < auto_low:
        auto_high = float("inf")
    return float(auto_low), float(auto_high), bg_mean, bg_std


def _perimeter(mask):
    try:
        from skimage.measure import perimeter_crofton
        return float(perimeter_crofton(mask.astype(bool), directions=4))
    except Exception:
        try:
            from skimage.measure import perimeter
            return float(perimeter(mask.astype(bool), neighborhood=8))
        except Exception:
            eroded = _scipy_ndi.binary_erosion(mask)
            return float(np.count_nonzero(mask & ~eroded))


def _circularity(mask):
    area = int(np.count_nonzero(mask))
    if area <= 0:
        return 0.0
    perim = _perimeter(mask)
    if perim <= 0:
        return 0.0
    return float(4.0 * np.pi * area / (perim * perim))


def _main_channel_for_cell(label_mask, marker_channels, channel_names, channel_thresholds):
    best_name = ""
    best_score = -1.0
    best_mean = 0.0
    best_low = 0.0
    for name, arr in zip(channel_names, marker_channels):
        vals = np.asarray(arr)[label_mask]
        if vals.size == 0:
            score = 0.0
            mean_val = 0.0
        else:
            mean_val = float(np.mean(vals))
            low = float(channel_thresholds.get(name, {}).get("auto_low", 0.0) or 0.0)
            score = mean_val / max(low, 1.0e-6)
        if score > best_score:
            best_score = score
            best_name = name
            best_mean = mean_val
            best_low = float(channel_thresholds.get(name, {}).get("auto_low", 0.0) or 0.0)
    return best_name, best_low, best_mean


def _connectivity_cleanup(labels, nuclei, n_labels):
    final = np.asarray(labels, dtype=np.uint32).copy()
    object_slices = _scipy_ndi.find_objects(final, max_label=n_labels)
    structure = np.ones((3, 3), dtype=bool)
    removed = 0
    for lab in range(1, n_labels + 1):
        slc = object_slices[lab - 1] if lab - 1 < len(object_slices) else None
        if slc is None:
            continue
        local = final[slc] == lab
        if not np.any(local):
            continue
        comps, _n = _scipy_ndi.label(local, structure=structure)
        touching = np.unique(comps[(nuclei[slc] == lab) & local])
        touching = touching[touching > 0]
        keep = np.isin(comps, touching) if touching.size else np.zeros_like(local, dtype=bool)
        drop = local & ~keep
        removed += int(np.count_nonzero(drop))
        local_final = final[slc]
        local_final[drop] = 0
    final[nuclei > 0] = nuclei[nuclei > 0]
    return final, removed


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
    """Run CSD and return a dict compatible with run_hq2_segmentation()."""
    started = time.perf_counter()
    p = _params(params)
    xp, xndi, backend_name = _xp_backend(p)
    timings = {}
    warnings = []

    nuclei = np.asarray(nuclei_labels, dtype=np.uint32)
    n_labels = int(nuclei.max())
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
            })
        return result

    marker_channels = [np.asarray(arr) for arr in (marker_channels or [])]
    channel_names = list(channel_names or [])[:len(marker_channels)]
    if not channel_names:
        channel_names = [f"channel_{idx + 1}" for idx in range(len(marker_channels))]

    def log_msg(msg):
        if logger is not None:
            try:
                logger.info(msg)
            except Exception:
                pass
        _emit(progress_callback, msg)

    try:
        _check_safety(p, started, cancel_check, "CSD-start")
        t0 = time.perf_counter()
        nuclei_x = xp.asarray(nuclei)
        nuclei_mask_x = nuclei_x > 0
        shrink_iter = max(0, int(round(float(p.get("nucleus_shrink", 3) or 0))))
        if shrink_iter > 0:
            eroded_x = xndi.binary_erosion(nuclei_mask_x, iterations=shrink_iter)
        else:
            eroded_x = nuclei_mask_x
        shrunk_labels = np.where(_asnumpy(eroded_x), nuclei, 0).astype(np.uint32, copy=False)
        timings["nucleus_shrink_seconds"] = time.perf_counter() - t0

        _check_safety(p, started, cancel_check, "CSD-distance")
        t0 = time.perf_counter()
        dist_x = xndi.distance_transform_edt(~nuclei_mask_x)
        donut_size = float(p.get("donut_size", 40) or 40)
        donut_mask_x = (dist_x > 0) & (dist_x <= donut_size)
        far_bg = _asnumpy(dist_x > (donut_size * 1.5))
        dist_from_nuc = _asnumpy(dist_x).astype(np.float32, copy=False)
        donut_mask = _asnumpy(donut_mask_x).astype(bool, copy=False)
        timings["donut_distance_seconds"] = time.perf_counter() - t0

        _check_safety(p, started, cancel_check, "CSD-gating")
        t0 = time.perf_counter()
        gated_union_x = xp.zeros(nuclei.shape, dtype=bool)
        channel_thresholds = {}
        gated_counts = {}
        for name, raw in zip(channel_names, marker_channels):
            auto_low, auto_high, bg_mean, bg_std = _channel_threshold(
                raw, far_bg, p.get("bg_sigma_factor", 3.0), p.get("saturation_percentile", 99.8)
            )
            raw_x = xp.asarray(raw)
            channel_gated_x = donut_mask_x & (raw_x >= auto_low) & (raw_x <= auto_high)
            gated_union_x |= channel_gated_x
            gated_counts[name] = int(_asnumpy(xp.count_nonzero(channel_gated_x)))
            channel_thresholds[name] = {
                "auto_low": float(auto_low),
                "auto_high": float(auto_high),
                "bg_mean": float(bg_mean),
                "bg_std": float(bg_std),
                "gated_pixels": int(gated_counts[name]),
            }
        gated_union = _asnumpy(gated_union_x).astype(bool, copy=False)
        timings["gating_seconds"] = time.perf_counter() - t0

        _check_safety(p, started, cancel_check, "CSD-watershed")
        t0 = time.perf_counter()
        gated_region_x = gated_union_x | nuclei_mask_x
        gated_region = _asnumpy(gated_region_x).astype(bool, copy=False)
        labels, watershed_backend = _watershed_with_gpu_preference(
            dist_x,
            nuclei_x,
            gated_region_x,
            dist_from_nuc,
            nuclei,
            gated_region,
        )
        labels[~gated_region] = 0
        labels[nuclei > 0] = nuclei[nuclei > 0]
        timings["watershed_seconds"] = time.perf_counter() - t0

        _check_safety(p, started, cancel_check, "CSD-connectivity")
        t0 = time.perf_counter()
        final, disconnected_removed = _connectivity_cleanup(labels, nuclei, n_labels)
        timings["connectivity_cleanup_seconds"] = time.perf_counter() - t0

        _check_safety(p, started, cancel_check, "CSD-shape-qc")
        t0 = time.perf_counter()
        fallback_expand = _expand_labels(nuclei, float(p.get("circularity_fallback_expand", 3) or 0))
        nucleus_area = np.bincount(nuclei.ravel(), minlength=n_labels + 1)
        gated_area = np.bincount(labels.ravel(), minlength=n_labels + 1)
        final_area_before = np.bincount(final.ravel(), minlength=n_labels + 1)
        qc_rows = []
        fallback_count = 0
        added_pixels_total = 0
        conflict_total = 0
        for lab in range(1, n_labels + 1):
            nuc_area = int(nucleus_area[lab]) if lab < len(nucleus_area) else 0
            init_area = int(gated_area[lab]) if lab < len(gated_area) else 0
            cell_mask = final == lab
            final_area = int(final_area_before[lab]) if lab < len(final_area_before) else int(np.count_nonzero(cell_mask))
            ratio = float(final_area) / max(float(nuc_area), 1.0)
            circ = _circularity(cell_mask)
            fallback = False
            low_conf = False
            reason = ""
            if (
                circ > float(p.get("max_circularity", 0.92) or 0.92)
                and ratio > float(p.get("circularity_ratio_threshold", 3.0) or 3.0)
            ):
                final[final == lab] = 0
                final[fallback_expand == lab] = lab
                cell_mask = final == lab
                final_area = int(np.count_nonzero(cell_mask))
                ratio = float(final_area) / max(float(nuc_area), 1.0)
                fallback = True
                low_conf = True
                reason = "circularity_fallback"
                fallback_count += 1

            added_mask = cell_mask & (nuclei != lab)
            added_area = int(np.count_nonzero(added_mask))
            added_pixels_total += added_area
            main_channel, local_threshold, mean_signal = _main_channel_for_cell(
                added_mask if np.any(added_mask) else cell_mask,
                marker_channels,
                channel_names,
                channel_thresholds,
            )
            qc_rows.append({
                "cell_id": lab,
                "nucleus_area": nuc_area,
                "initial_hq_area": init_area,
                "refined_area": final_area,
                "added_area": added_area,
                "added_area_fraction": float(added_area) / max(float(init_area), 1.0),
                "cell_to_nucleus_ratio": float(ratio),
                "main_channel": main_channel,
                "local_threshold": float(local_threshold),
                "mean_refine_signal": float(mean_signal),
                "conflict_pixel_count": 0,
                "refinement_applied": bool(added_area > 0),
                "fallback_to_hq": bool(fallback),
                "low_confidence_flag": bool(low_conf),
                "low_confidence_reason": reason,
            })
        final[nuclei > 0] = nuclei[nuclei > 0]
        timings["shape_qc_seconds"] = time.perf_counter() - t0

    except CSDFallback as exc:
        warnings.append(str(exc))
        final = nuclei.copy()
        labels = nuclei.copy()
        shrunk_labels = nuclei.copy()
        gated_union = np.zeros_like(nuclei, dtype=bool)
        channel_thresholds = {}
        watershed_backend = "fallback_not_run"
        qc_rows = []
        fallback_count = n_labels
        disconnected_removed = 0
        added_pixels_total = 0
        conflict_total = 0
        log_msg(f"[CSD] fallback to nuclei only: {exc}")
    except Exception as exc:
        if backend_name == "cupy":
            cpu_params = dict(p)
            cpu_params["use_gpu"] = False
            log_msg(f"[CSD] GPU path failed, retrying CPU: {exc}")
            return run_constrained_donut_segmentation(
                nuclei,
                marker_channels,
                channel_names,
                cpu_params,
                logger=logger,
                return_layers=return_layers,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        raise

    expansion_added = np.where((final > 0) & (nuclei == 0), final, 0).astype(np.uint32, copy=False)
    timings["total_seconds"] = time.perf_counter() - started
    peak_mb = _rss_mb()
    stats = {
        "cells_refined": int(sum(1 for row in qc_rows if row.get("refinement_applied"))),
        "cells_fallback": int(fallback_count),
        "added_pixels_total": int(added_pixels_total),
        "disconnected_pixels_removed": int(disconnected_removed),
        "conflict_pixels_total": int(conflict_total),
        "gated_pixels_total": int(np.count_nonzero(gated_union)),
    }
    metadata = {
        "timings": timings,
        "fallback": bool(warnings),
        "fallback_reason": "; ".join(warnings),
        "warnings": warnings,
        "peak_memory_estimate_mb": peak_mb,
        "csd_backend": backend_name,
        "watershed_backend": watershed_backend,
        "csd_mode": "constrained_signal_donut",
        "hq2_mode": "constrained_signal_donut",
        "imagej_proposal_enabled": False,
        "channel_thresholds": channel_thresholds,
        "fallback_count": int(fallback_count),
        "overexpanded_cell_count": int(fallback_count),
        "refinement_added_pixels": int(np.count_nonzero(expansion_added)),
    }
    log_msg(
        f"[CSD] final labels={int(final.max())} gated={stats['gated_pixels_total']} "
        f"added={stats['added_pixels_total']} fallback={fallback_count}"
    )

    result = {
        "final_labels": final.astype(np.uint32, copy=False),
        "nuclei_labels": nuclei.astype(np.uint32, copy=False),
        "qc_rows": qc_rows,
        "stats": stats,
        "metadata": metadata,
    }
    if return_layers:
        zeros = np.zeros_like(nuclei, dtype=np.uint32)
        gated_region_labels = np.where(gated_union, labels, 0).astype(np.uint32, copy=False)
        result.update({
            "hq_proposal_labels": gated_region_labels,
            "imagej_proposal_labels": zeros,
            "high_confidence_core_labels": shrunk_labels.astype(np.uint32, copy=False),
            "expansion_added_pixels": expansion_added,
            "refinement_added_pixels": expansion_added.copy(),
        })
    return result


def write_csd_qc_table(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HQ2_QC_FIELDS)
        writer.writeheader()
        for row in rows or []:
            writer.writerow({k: row.get(k, "") for k in HQ2_QC_FIELDS})


def csd_metadata_fields(params, paths):
    runtime = dict(params.get("csd_runtime_metadata") or params.get("hq2_runtime_metadata") or {})
    return {
        "method": "cellpose_nuclei_csd",
        "display_name": "Cellpose nuclei + CSD",
        "hq2_mode": "constrained_signal_donut",
        "csd_mode": "constrained_signal_donut",
        "imagej_proposal_enabled": False,
        "nuclei_method": "cellpose_nuclei_dapi",
        "hq_channels": parse_hq_channels(params.get("hq_channels") or []),
        "hq_input_mode": params.get("hq_input_mode", "selected_channels_from_source"),
        "csd_parameters": {
            "donut_size": params.get("donut_size", 40),
            "nucleus_shrink": params.get("nucleus_shrink", 3),
            "bg_sigma_factor": params.get("bg_sigma_factor", 3.0),
            "saturation_percentile": params.get("saturation_percentile", 99.8),
            "max_circularity": params.get("max_circularity", 0.92),
            "circularity_ratio_threshold": params.get("circularity_ratio_threshold", 3.0),
            "circularity_fallback_expand": params.get("circularity_fallback_expand", 3),
            "timeout_seconds": params.get("timeout_seconds", 600),
            "memory_guard_mb": params.get("memory_guard_mb", 0),
        },
        "channel_thresholds": runtime.get("channel_thresholds", {}),
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
        "csd_runtime_metadata": runtime,
        "hq2_runtime_metadata": runtime,
    }


def save_csd_outputs(output_dir, prefix, result, params):
    os.makedirs(output_dir, exist_ok=True)
    paths = {}
    mapping = {
        "nuclei_mask_path": "nuclei_labels",
        "hq_proposal_mask_path": "hq_proposal_labels",
        "initial_hq_mask_path": "hq_proposal_labels",
        "imagej_proposal_mask_path": "imagej_proposal_labels",
        "core_mask_path": "high_confidence_core_labels",
        "expansion_mask_path": "expansion_added_pixels",
        "refinement_added_pixels_path": "refinement_added_pixels",
        "final_cell_mask_path": "final_labels",
    }
    for path_key, result_key in mapping.items():
        path = os.path.join(output_dir, f"{prefix}_{result_key}.npy")
        np.save(path, np.asarray(result[result_key], dtype=np.uint32))
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
    print(f"[CSD] outputs saved to={output_dir}")
    return paths, meta_path
