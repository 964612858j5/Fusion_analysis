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

# ── GPU availability ──────────────────────────────────────────────────

try:
    import cupy as cp
    import cupyx.scipy.ndimage as _cupyx_ndi
    CUCIM_AVAILABLE = True
    CUCIM_IMPORT_ERROR = ""
    print(f"[GPU] cupy {cp.__version__} ready, using cupyx.scipy.ndimage for morphology")
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
    if CUCIM_AVAILABLE:
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
            print(f"[GPU tophat fallback] {_e}")
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
    if prefer_gpu and CUCIM_AVAILABLE:
        try:
            gpu_arr = cp.asarray(arr32)
            bg_gpu  = _cupyx_ndi.gaussian_filter(gpu_arr, sigma=sigma, mode='reflect')
            out_gpu = cp.clip(gpu_arr - bg_gpu, 0, None)
            out = cp.asnumpy(out_gpu).astype(np.float32, copy=False)
            del gpu_arr, bg_gpu, out_gpu
            return out
        except Exception as _e:
            print(f"[GPU cucim fallback] {_e}")
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

    overlap = max(1, 2 * param)
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
