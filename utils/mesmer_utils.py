"""Mesmer backend helpers for Step1/Step2 segmentation.

All heavy DeepCell imports are lazy so missing dependencies produce a clear
runtime error instead of breaking GUI startup.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import time
import traceback
from dataclasses import dataclass
from typing import Any

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


class MesmerUnavailableError(RuntimeError):
    pass


@dataclass
class MesmerDeviceStatus:
    tensorflow_available: bool
    deepcell_available: bool
    mesmer_available: bool
    tensorflow_version: str = ""
    deepcell_version: str = ""
    gpu_devices: list[str] | None = None
    device_requested: str = "auto"
    device_used: str = "cpu"
    fallback_reason: str = ""
    error: str = ""

    def as_dict(self):
        return {
            "tensorflow_available": self.tensorflow_available,
            "deepcell_available": self.deepcell_available,
            "mesmer_available": self.mesmer_available,
            "tensorflow_version": self.tensorflow_version,
            "deepcell_version": self.deepcell_version,
            "gpu_devices": list(self.gpu_devices or []),
            "device_requested": self.device_requested,
            "device_used": self.device_used,
            "fallback_reason": self.fallback_reason,
            "error": self.error,
        }


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except Exception:
        return ""


def get_mesmer_device_status(use_gpu: str | bool = "auto", logger=None) -> MesmerDeviceStatus:
    requested = str(use_gpu).lower()
    if requested == "true":
        requested = "gpu"
    if requested == "false":
        requested = "cpu"

    status = MesmerDeviceStatus(
        tensorflow_available=False,
        deepcell_available=False,
        mesmer_available=False,
        tensorflow_version="",
        deepcell_version=_version("deepcell"),
        gpu_devices=[],
        device_requested=requested,
        device_used="cpu",
    )

    def log(msg):
        print(msg)
        if logger is not None:
            try:
                logger.info(msg)
            except Exception:
                pass

    try:
        tf = importlib.import_module("tensorflow")
        status.tensorflow_available = True
        status.tensorflow_version = getattr(tf, "__version__", "")
        log(f"[Mesmer] tensorflow version={status.tensorflow_version}")
    except Exception as exc:
        status.error = f"TensorFlow import failed: {exc}"
        status.fallback_reason = status.error
        log(f"[Mesmer] tensorflow import failed={exc}")
        return status

    try:
        importlib.import_module("deepcell")
        from deepcell.applications import Mesmer  # noqa: F401

        status.deepcell_available = True
        status.mesmer_available = True
    except Exception as exc:
        status.error = "DeepCell/Mesmer is not installed in the current environment."
        status.fallback_reason = str(exc)
        log(f"[Mesmer] deepcell available=False")
        log(status.error)
        return status

    log("[Mesmer] deepcell available=True")
    log(f"[Mesmer] device requested={requested}")
    if requested == "cpu":
        try:
            tf.config.set_visible_devices([], "GPU")
        except Exception:
            pass
        status.device_used = "cpu"
        log("[Mesmer] device used=cpu")
        return status

    try:
        gpus = tf.config.list_physical_devices("GPU")
        status.gpu_devices = [str(g) for g in gpus]
        log(f"[Mesmer] gpu devices={status.gpu_devices}")
        if gpus:
            for gpu in gpus:
                try:
                    tf.config.experimental.set_memory_growth(gpu, True)
                except Exception as exc:
                    log(f"[Mesmer] memory growth warning={exc}")
            status.device_used = "gpu"
        else:
            status.device_used = "cpu"
            status.fallback_reason = "TensorFlow sees no GPU"
            log("[Mesmer] fallback to CPU reason=TensorFlow sees no GPU")
    except Exception as exc:
        status.device_used = "cpu"
        status.fallback_reason = f"TensorFlow GPU initialization failed: {exc}"
        log(f"[Mesmer] fallback to CPU reason={status.fallback_reason}")
        try:
            tf.config.set_visible_devices([], "GPU")
        except Exception:
            pass
    log(f"[Mesmer] device used={status.device_used}")
    return status


def normalize_percentile(arr, low=1.0, high=99.8, eps=1e-6):
    arr = np.asarray(arr, dtype=np.float32)
    if arr.size == 0:
        return arr.astype(np.float32)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros_like(arr, dtype=np.float32)
    lo, hi = np.percentile(finite, [float(low), float(high)])
    if hi <= lo:
        vmax = float(np.nanmax(arr))
        return np.clip(arr / vmax, 0.0, 1.0).astype(np.float32) if vmax > 0 else np.zeros_like(arr, dtype=np.float32)
    return np.clip((arr - lo) / (hi - lo + eps), 0.0, 1.0).astype(np.float32)


def parse_channel_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.replace(",", ";").split(";") if x.strip()]
    return [str(x).strip() for x in value if str(x).strip()]


def _read_channel(channel_source: Any, channel: str, bbox=None):
    if channel_source is None:
        raise ValueError("channel_source is required")
    if isinstance(channel_source, dict):
        if channel not in channel_source:
            raise KeyError(f"Channel not found: {channel}")
        arr = channel_source[channel]
        if bbox is not None:
            y0, y1, x0, x1 = [int(v) for v in bbox]
            arr = arr[y0:y1, x0:x1]
        return np.asarray(arr)
    if hasattr(channel_source, "read_region"):
        if bbox is None:
            raise ValueError("bbox is required for OMETIFFLoader channel_source")
        y0, y1, x0, x1 = [int(v) for v in bbox]
        return channel_source.read_region(channel, y0, y1, x0, x1, downsample=1)
    if hasattr(channel_source, "__getitem__"):
        return np.asarray(channel_source[channel])
    raise TypeError(f"Unsupported channel_source: {type(channel_source)!r}")


def build_mesmer_input(
    channel_source,
    nuclear_channel="DAPI",
    membrane_channels=None,
    weights=None,
    bbox=None,
    normalize=True,
    percentile_low=1.0,
    percentile_high=99.8,
    input_mode="selected_channels",
    fusion_image=None,
):
    membrane_channels = parse_channel_list(membrane_channels)
    weights = dict(weights or {})
    print(f"[Mesmer] nuclear channel={nuclear_channel}")
    print(f"[Mesmer] membrane channels={membrane_channels}")

    nuclear = _read_channel(channel_source, nuclear_channel, bbox=bbox)
    nuclear = normalize_percentile(nuclear, percentile_low, percentile_high) if normalize else np.asarray(nuclear, dtype=np.float32)

    second = None
    mode = str(input_mode or "selected_channels").lower()
    if mode in {"dapi only", "dapi_only"}:
        second = np.zeros_like(nuclear, dtype=np.float32)
    elif mode in {"dapi + weighted fusion", "weighted_fusion", "step1_weighted_fusion"} and fusion_image is not None:
        second = np.asarray(fusion_image, dtype=np.float32)
        if normalize:
            second = normalize_percentile(second, percentile_low, percentile_high)
    else:
        for ch in membrane_channels:
            arr = _read_channel(channel_source, ch, bbox=bbox)
            arr = normalize_percentile(arr, percentile_low, percentile_high) if normalize else np.asarray(arr, dtype=np.float32)
            arr = arr * float(weights.get(ch, 1.0))
            second = arr if second is None else np.maximum(second, arr)
        if second is None:
            second = np.zeros_like(nuclear, dtype=np.float32)

    batch = np.stack([nuclear, second.astype(np.float32)], axis=-1)
    batch = np.expand_dims(batch.astype(np.float32, copy=False), axis=0)
    print(f"[Mesmer] input shape={batch.shape}")
    return batch


def build_mesmer_input_from_fused_tile(tile_data, normalize=True, percentile_low=1.0, percentile_high=99.8):
    tile = np.asarray(tile_data)
    if tile.ndim != 3 or tile.shape[-1] < 2:
        raise ValueError(f"Expected fused tile shape H,W,2+, got {tile.shape}")
    membrane = tile[:, :, 0]
    nuclear = tile[:, :, 1]
    if normalize:
        nuclear = normalize_percentile(nuclear, percentile_low, percentile_high)
        membrane = normalize_percentile(membrane, percentile_low, percentile_high)
    batch = np.stack([nuclear.astype(np.float32), membrane.astype(np.float32)], axis=-1)
    batch = np.expand_dims(batch, axis=0)
    print("[Mesmer] nuclear channel=DAPI/fused_zarr_channel_1")
    print("[Mesmer] membrane channels=['fused_zarr_channel_0']")
    print(f"[Mesmer] input shape={batch.shape}")
    return batch


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


def _default_mesmer_model_path():
    candidates = [
        os.environ.get("DEEPCELL_MESMER_MODEL_PATH"),
        "/sda1/Fusion/benchmark/spacec/models/Mesmer_model/MultiplexSegmentation",
        os.path.join(os.path.expanduser("~"), ".deepcell", "models", "MultiplexSegmentation"),
    ]
    for path in candidates:
        if path and os.path.exists(os.path.join(path, "saved_model.pb")):
            return path
    return ""


def load_mesmer_application(model_path=None):
    try:
        from deepcell.applications import Mesmer
    except Exception as exc:
        raise MesmerUnavailableError("DeepCell/Mesmer is not installed in the current environment.") from exc
    model_path = model_path or _default_mesmer_model_path()
    if model_path:
        try:
            import tensorflow as tf
            print(f"[Mesmer] loading local model={model_path}")
            model = tf.keras.models.load_model(model_path)
            return Mesmer(model=model)
        except Exception as exc:
            raise MesmerUnavailableError(f"Failed to load local Mesmer model from {model_path}: {exc}") from exc
    return Mesmer()


def run_mesmer_prediction(input_batch, mode="whole_cell", image_mpp=0.5, batch_size=1, app=None):
    app = app or load_mesmer_application()
    mode = str(mode or "whole_cell").lower()
    compartment = "nuclear" if mode in {"nuclei", "nuclear"} else "whole-cell"
    print("[Mesmer] prediction started")
    started = time.perf_counter()
    pred = app.predict(input_batch, image_mpp=float(image_mpp), compartment=compartment, batch_size=int(batch_size or 1))
    runtime = time.perf_counter() - started
    mask = np.squeeze(pred).astype(np.uint32)
    print("[Mesmer] prediction finished")
    print(f"[Mesmer] labels count={int(mask.max()) if mask.size else 0}")
    return mask, runtime


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
