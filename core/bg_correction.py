"""
block01/core/bg_correction.py — Background correction utilities.
"""

import os
import json
import numpy as np
from skimage.filters import gaussian as sk_gaussian, threshold_otsu
from skimage.morphology import white_tophat, disk

from ..config import (
    TOPHAT_RADIUS_DEFAULT,
    CUCIM_SIGMA_DEFAULT,
    BG_CORR_MAX_TILE,
)

# Bump on ANY numeric change of the correction methods (cache keys and
# provenance depend on it). "2": gaussian/cucim tiled halo widened from
# 2*sigma to ceil(4*sigma) (full filter support, truncate=4) — tophat
# unchanged at 2*radius. Saved outputs produced by version "1" can differ
# from "2" near internal tile borders of the gaussian method.
BG_CORRECTION_ALGO_VERSION = "2"


def method_overlap(method, param):
    """Tile halo required by a correction method — the SINGLE source of truth
    for both the preview path (_apply_background_method_tiled) and the
    production WsiCorrectionWorker tiling.

    - tophat: erosion+dilation reach exactly 2*radius;
    - cucim/gaussian: effective support is truncate*sigma with truncate=4
      (cupyx/scipy/skimage default). The version-"1" uniform 2*param overlap
      truncated the gaussian's support and could leave faint seams at tile
      borders (verified by tests/test_bg_correction_halo.py).
    """
    param = max(1, int(param))
    if str(method).strip().lower() == "cucim":
        return int(np.ceil(4 * param))
    return 2 * param

# ── GPU availability ──────────────────────────────────────────────────
_GPU_FAILURE_CACHE = set()
GPU_MORPH_AVAILABLE = False
GPU_MORPH_SMOKE_TEST = {"cupy": "not_run", "cupyx_ndimage": "not_run", "cucim": "not_run"}


def _gpu_failure_key(exc):
    text = str(exc)
    if "cudaErrorCompatNotSupportedOnDevice" in text or "forward compatibility was attempted on non supported HW" in text:
        return "cuda_compat_not_supported"
    return text.splitlines()[0][:160] if text else exc.__class__.__name__


def _warn_gpu_once(key, message):
    if key in _GPU_FAILURE_CACHE:
        return
    _GPU_FAILURE_CACHE.add(key)
    print(message)


def _disable_gpu_morph(exc, context):
    global GPU_MORPH_AVAILABLE, CUCIM_AVAILABLE
    GPU_MORPH_AVAILABLE = False
    CUCIM_AVAILABLE = False
    key = _gpu_failure_key(exc)
    _warn_gpu_once(
        key,
        f"[GPU] {context} disabled for this session due to {key}: {exc}\n"
        "[GPU] Falling back to CPU morphology.",
    )


def _run_gpu_morph_smoke_test():
    arr = cp.asarray(np.arange(64, dtype=np.float32).reshape(8, 8))
    arr = arr + cp.float32(1.0)
    _ = cp.asnumpy(arr)
    GPU_MORPH_SMOKE_TEST["cupy"] = "pass"
    eroded = _cupyx_ndi.grey_erosion(arr, size=(3, 3), mode="reflect")
    dilated = _cupyx_ndi.grey_dilation(eroded, size=(3, 3), mode="reflect")
    gauss = _cupyx_ndi.gaussian_filter(arr, sigma=1, mode="reflect")
    _ = cp.asnumpy(dilated + gauss)
    GPU_MORPH_SMOKE_TEST["cupyx_ndimage"] = "pass"
    try:
        import cucim  # noqa: F401
        GPU_MORPH_SMOKE_TEST["cucim"] = "import_pass"
    except Exception as exc:
        GPU_MORPH_SMOKE_TEST["cucim"] = f"import_fail: {exc}"


try:
    import cupy as cp
    import cupyx.scipy.ndimage as _cupyx_ndi
    CUCIM_IMPORT_ERROR = ""
    try:
        _run_gpu_morph_smoke_test()
        GPU_MORPH_AVAILABLE = True
        CUCIM_AVAILABLE = True
        print(f"[GPU] cupy {cp.__version__} ready, GPU morphology smoke test passed")
    except Exception as _smoke_exc:
        CUCIM_IMPORT_ERROR = str(_smoke_exc)
        CUCIM_AVAILABLE = False
        GPU_MORPH_AVAILABLE = False
        _warn_gpu_once(
            _gpu_failure_key(_smoke_exc),
            f"[GPU] CuPy import ok but GPU morphology smoke test failed: {_smoke_exc}\n"
            "[GPU] Falling back to CPU morphology.",
        )
except Exception as _cucim_exc:
    cp = None
    _cupyx_ndi = None
    CUCIM_AVAILABLE = False
    CUCIM_IMPORT_ERROR = str(_cucim_exc)
    print(f"[GPU] cupy not available, using CPU: {_cucim_exc}")



def _normalize_correction_config(cfg):
    if not cfg:
        return None
    method_params = dict(cfg.get("method_params") or {})
    channel_decisions = dict(cfg.get("channel_decisions") or {})
    return {
        "method_params": {
            "tophat_radius": int(method_params.get("tophat_radius", TOPHAT_RADIUS_DEFAULT)),
            "cucim_sigma": int(method_params.get("cucim_sigma", CUCIM_SIGMA_DEFAULT)),
        },
        "channel_decisions": {
            str(k): str(v).strip().lower()
            for k, v in channel_decisions.items()
            if str(v).strip().lower() in {"tophat", "cucim", "original"}
        },
    }


def _load_correction_config(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _normalize_correction_config(json.load(f))
    except Exception:
        return None


def _safe_otsu(arr):
    arr = np.asarray(arr, dtype=np.float32)
    finite = arr[np.isfinite(arr)]
    if finite.size < 2:
        return 0.0
    if float(np.max(finite)) <= float(np.min(finite)):
        return float(finite.flat[0])
    try:
        return float(threshold_otsu(finite))
    except Exception:
        return float(np.mean(finite))


def _compute_bg_metrics(arr):
    arr = np.asarray(arr, dtype=np.float32)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"snr": 0.0, "bg_cv": 0.0}

    thr = _safe_otsu(arr)
    fg = arr[arr > thr]
    bg = arr[arr <= thr]
    if fg.size == 0:
        fg = arr
    if bg.size == 0:
        bg = arr

    fg_mean = float(np.mean(fg)) if fg.size else 0.0
    bg_mean = float(np.mean(bg)) if bg.size else 0.0
    bg_std = float(np.std(bg)) if bg.size else 0.0
    snr = fg_mean / max(bg_std, 1e-6)
    bg_cv = bg_std / max(bg_mean, 1e-6)
    return {"snr": snr, "bg_cv": bg_cv}


def _tile_slices(height, width, tile_size, overlap):
    step = max(1, int(tile_size))
    pad = max(0, int(overlap))
    for y in range(0, height, step):
        for x in range(0, width, step):
            y_core0 = y
            y_core1 = min(height, y + step)
            x_core0 = x
            x_core1 = min(width, x + step)
            y_pad0 = max(0, y_core0 - pad)
            y_pad1 = min(height, y_core1 + pad)
            x_pad0 = max(0, x_core0 - pad)
            x_pad1 = min(width, x_core1 + pad)
            crop_y0 = y_core0 - y_pad0
            crop_y1 = crop_y0 + (y_core1 - y_core0)
            crop_x0 = x_core0 - x_pad0
            crop_x1 = crop_x0 + (x_core1 - x_core0)
            yield (
                (y_core0, y_core1, x_core0, x_core1),
                (y_pad0, y_pad1, x_pad0, x_pad1),
                (crop_y0, crop_y1, crop_x0, crop_x1),
            )


def _apply_tophat_gpu_or_cpu(arr, radius):
    """White TopHat background subtraction.
    GPU path: cupyx.scipy.ndimage.grey_erosion/dilation (no NVRTC JIT).
    CPU path: skimage white_tophat fallback.
    Border mode: 'reflect'.
    """
    radius = max(1, int(radius))
    arr32 = arr.astype(np.float32, copy=False)
    if GPU_MORPH_AVAILABLE:
        try:
            size = 2 * radius + 1
            gpu_arr = cp.asarray(arr32)
            eroded  = _cupyx_ndi.grey_erosion(gpu_arr,  size=(size, size), mode='reflect')
            dilated = _cupyx_ndi.grey_dilation(eroded,  size=(size, size), mode='reflect')
            tophat  = cp.clip(gpu_arr - dilated, 0, None)
            out = cp.asnumpy(tophat).astype(np.float32, copy=False)
            del gpu_arr, eroded, dilated, tophat
            return out
        except Exception as _e:
            _disable_gpu_morph(_e, "tophat")
    return white_tophat(arr32, footprint=disk(radius), mode='reflect').astype(np.float32)


# Alias kept for backward compatibility — WsiCorrectionWorker etc. call _apply_tophat_cpu
def _apply_tophat_cpu(arr, radius):
    return _apply_tophat_gpu_or_cpu(arr, radius)


def _apply_cucim_or_cpu(arr, sigma, prefer_gpu=True):
    """Gaussian background estimation and subtraction.
    GPU path: cupyx.scipy.ndimage.gaussian_filter.
    CPU path: skimage gaussian fallback.
    Border mode: 'reflect'.
    """
    sigma = max(1, int(sigma))
    arr32 = arr.astype(np.float32, copy=False)
    if prefer_gpu and GPU_MORPH_AVAILABLE:
        try:
            gpu_arr = cp.asarray(arr32)
            bg_gpu  = _cupyx_ndi.gaussian_filter(gpu_arr, sigma=sigma, mode='reflect')
            out_gpu = cp.clip(gpu_arr - bg_gpu, 0, None)
            out = cp.asnumpy(out_gpu).astype(np.float32, copy=False)
            del gpu_arr, bg_gpu, out_gpu
            return out
        except Exception as _e:
            _disable_gpu_morph(_e, "cucim")
    bg = sk_gaussian(arr32, sigma=sigma, preserve_range=True, mode='reflect')
    return np.clip(arr32 - bg.astype(np.float32, copy=False), 0, None).astype(np.float32)


def _apply_background_method_tiled(arr, method, radius=None, sigma=None,
                                   tile_size=BG_CORR_MAX_TILE, prefer_gpu=True):
    arr32 = np.asarray(arr, dtype=np.float32)
    if arr32.ndim != 2 or arr32.size == 0:
        return arr32.copy()

    method = (method or "original").lower()
    if method == "original":
        return arr32.copy()

    if method == "tophat":
        param = max(1, int(radius if radius is not None else TOPHAT_RADIUS_DEFAULT))
    elif method == "cucim":
        param = max(1, int(sigma if sigma is not None else CUCIM_SIGMA_DEFAULT))
    else:
        return arr32.copy()

    overlap = method_overlap(method, param)
    h, w = arr32.shape
    out = np.zeros((h, w), dtype=np.float32)

    for core, padded, crop in _tile_slices(h, w, tile_size, overlap):
        y0, y1, x0, x1 = core
        py0, py1, px0, px1 = padded
        cy0, cy1, cx0, cx1 = crop
        tile = arr32[py0:py1, px0:px1]
        if method == "tophat":
            corr = _apply_tophat_cpu(tile, radius=param)
        else:
            corr = _apply_cucim_or_cpu(tile, sigma=param, prefer_gpu=prefer_gpu)
        out[y0:y1, x0:x1] = corr[cy0:cy1, cx0:cx1]
    return out


# ── v14.4 corrected_channels.zarr provenance + validity ──────────────────────
# Provenance for the Step0 Background Correction output. Local constants in the
# background-correction layer (the corrected zarr is a different output kind than
# the channel-remap configs, which have their own registry). This is a
# preprocessing output only — it is NEVER marked step2_ready and never touches
# HQ2/CSD source policy / promotion / resolver.
CREATED_FROM_STEP0_BACKGROUND_CORRECTION = "step0_background_correction"
CORRECTED_ZARR_OUTPUT_KIND = "corrected_channels_zarr"
CORRECTED_ZARR_USED_FOR = "background_corrected_marker_images"


def stamp_corrected_zarr_provenance(group):
    """Stamp honest preprocessing provenance onto a corrected_channels.zarr root.

    `group` is an open writable zarr group. Never sets step2_ready and never
    records anything source-aware — this is a plain preprocessing output."""
    group.attrs["created_from_step"] = CREATED_FROM_STEP0_BACKGROUND_CORRECTION
    group.attrs["output_kind"] = CORRECTED_ZARR_OUTPUT_KIND
    group.attrs["used_for"] = CORRECTED_ZARR_USED_FOR


# v14.5a: per-channel identity for a corrected zarr ARRAY (metadata only — the
# array bytes/shape are never touched). Makes each corrected channel array
# self-describing so a future (v14.5c) per-channel resolver can re-resolve and
# cross-validate a corrected channel's source identity. The intensity space of a
# background-corrected marker array (distinct from raw_ome_native_float).
CORRECTED_CHANNEL_INTENSITY_SPACE = "background_corrected_marker_image"


def stamp_corrected_channel_identity(ds, channel_name, channel_index=None,
                                     correction_method="unknown", roi_name=None,
                                     roi_bbox_fullres=None,
                                     correction_param_name=None,
                                     correction_param_value=None):
    """Stamp per-channel source identity onto a corrected channel zarr array.

    `ds` is an open writable zarr ARRAY (already created with its data). This
    only writes ds.attrs — it never recreates the array or changes its data or
    shape. `channel_index` may be None when it cannot be reliably derived; the
    stable channel_name / channel_key / shape / dtype are always recorded.
    Never marks the array step2_ready.

    `correction_param_name` / `correction_param_value` record the method-specific
    parameter (tophat -> tophat_radius, cucim -> cucim_sigma) so an incremental
    Save can detect a PARAMETER change, not just a method-name change. These are
    ADDITIVE fields — existing v14.5a attrs are unchanged.
    """
    ds.attrs["source_kind"] = "corrected_zarr"
    ds.attrs["channel_name"] = str(channel_name)
    ds.attrs["channel_key"] = str(channel_name)
    ds.attrs["channel_index"] = (int(channel_index)
                                 if channel_index is not None else None)
    ds.attrs["intensity_space"] = CORRECTED_CHANNEL_INTENSITY_SPACE
    ds.attrs["correction_method"] = str(correction_method or "unknown")
    ds.attrs["dtype"] = str(ds.dtype)
    ds.attrs["source_shape"] = [int(ds.shape[0]), int(ds.shape[1])]
    if roi_name is not None:
        ds.attrs["roi_name"] = str(roi_name)
    if roi_bbox_fullres is not None:
        ds.attrs["roi_bbox_fullres"] = [int(v) for v in roi_bbox_fullres]
    if correction_param_name is not None:
        ds.attrs["correction_param_name"] = str(correction_param_name)
    if correction_param_value is not None:
        ds.attrs["correction_param_value"] = int(correction_param_value)
    # Algorithm version participates in artifact identity: an incremental
    # Save must NOT reuse a channel computed by an older numeric version
    # (e.g. version "1" gaussian with the truncated 2*sigma halo).
    ds.attrs["bg_correction_algo_version"] = BG_CORRECTION_ALGO_VERSION


def corrected_zarr_report(path):
    """Inspect a corrected_channels.zarr and report whether it is a VALID,
    non-empty corrected output (vs a directory-only / empty group with zero
    channel arrays).

    A directory existing is NOT proof of a valid output. A valid corrected
    output must contain at least one channel array with a non-zero shape. The
    worker nests arrays as root[<roi_group>][<channel>]; this walks groups
    recursively and also accepts arrays directly under root.

    Returns a plain dict:
        exists           : bool — the zarr group could be opened
        n_channel_arrays : int  — number of non-empty channel arrays found
        channel_arrays   : list[str] — "<group>/<channel>" paths
        shapes           : dict[str, list[int]]
        non_empty        : bool — n_channel_arrays > 0
    """
    import zarr

    rep = {"exists": False, "n_channel_arrays": 0, "channel_arrays": [],
           "shapes": {}, "non_empty": False}
    if not path or not os.path.exists(path):
        return rep
    try:
        root = zarr.open_group(path, mode="r")
    except Exception:
        return rep
    rep["exists"] = True

    found = []

    def _collect(grp, prefix):
        for key in grp.array_keys():
            arr = grp[key]
            shp = [int(s) for s in (arr.shape or [])]
            if shp and all(s > 0 for s in shp):
                found.append((f"{prefix}{key}", shp))
        for gkey in grp.group_keys():
            _collect(grp[gkey], f"{prefix}{gkey}/")

    _collect(root, "")
    rep["channel_arrays"] = [name for name, _ in found]
    rep["shapes"] = {name: shp for name, shp in found}
    rep["n_channel_arrays"] = len(found)
    rep["non_empty"] = len(found) > 0
    return rep
