"""Mesmer worker helpers for Step1 patch preview and Step2 tiles."""

from __future__ import annotations

import json
import os
import time
import traceback

import numpy as np

from ..core.io_loader import OMETIFFLoader
from ..core.fusion_engine import FusionEngine
from ..utils.mesmer_utils import (
    MESMER_METHODS,
    MESMER_NUCLEAR_GUIDED,
    MESMER_NUCLEI,
    MESMER_WHOLE_CELL,
    build_mesmer_input,
    build_mesmer_input_from_fused_tile,
    get_mesmer_device_status,
    load_mesmer_application,
    mesmer_metadata,
    parse_channel_list,
    postprocess_mask,
    run_mesmer_prediction,
)


def method_to_mesmer_mode(method):
    if method == MESMER_NUCLEI:
        return "nuclei"
    return "whole_cell"


def run_mesmer_on_fused_tile(tile_data, params, app=None, logger=None):
    params = dict(params or {})
    status = get_mesmer_device_status(params.get("use_gpu", "auto"), logger=logger)
    if not status.mesmer_available:
        raise RuntimeError(status.error or "DeepCell/Mesmer is not installed in the current environment.")
    app = app or load_mesmer_application()
    print(f"[Mesmer] tile_size={params.get('tile_size')}")
    print(f"[Mesmer] overlap={params.get('overlap')}")
    batch = build_mesmer_input_from_fused_tile(
        tile_data,
        normalize=bool(params.get("normalize_input", True)),
        percentile_low=float(params.get("percentile_low", params.get("normalization_percentile_low", 1.0))),
        percentile_high=float(params.get("percentile_high", params.get("normalization_percentile_high", 99.8))),
    )
    return run_mesmer_on_batch(batch, params, app=app, status=status)


def run_mesmer_on_channel_source(channel_source, params, app=None, logger=None):
    params = dict(params or {})
    status = get_mesmer_device_status(params.get("use_gpu", "auto"), logger=logger)
    if not status.mesmer_available:
        raise RuntimeError(status.error or "DeepCell/Mesmer is not installed in the current environment.")
    app = app or load_mesmer_application()
    print(f"[Mesmer] tile_size={params.get('tile_size')}")
    print(f"[Mesmer] overlap={params.get('overlap')}")
    batch = build_mesmer_input(
        channel_source,
        nuclear_channel=params.get("nuclear_channel") or "DAPI",
        membrane_channels=parse_channel_list(params.get("membrane_channels")),
        weights=params.get("channel_weights") or params.get("weights") or {},
        normalize=bool(params.get("normalize_input", True)),
        percentile_low=float(params.get("percentile_low", params.get("normalization_percentile_low", 1.0))),
        percentile_high=float(params.get("percentile_high", params.get("normalization_percentile_high", 99.8))),
        input_mode=params.get("input_mode", "selected_channels"),
    )
    return run_mesmer_on_batch(batch, params, app=app, status=status)


def run_mesmer_on_batch(batch, params, app=None, status=None):
    params = dict(params or {})
    if status is None:
        status = get_mesmer_device_status(params.get("use_gpu", "auto"))
        if not status.mesmer_available:
            raise RuntimeError(status.error or "DeepCell/Mesmer is not installed in the current environment.")
    app = app or load_mesmer_application()
    mask, runtime = run_mesmer_prediction(
        batch,
        mode=method_to_mesmer_mode(params.get("method")),
        image_mpp=float(params.get("image_mpp", params.get("pixel_size", 0.5))),
        batch_size=int(params.get("batch_size", 1) or 1),
        app=app,
    )
    nuclei_mask = None
    if params.get("method") == MESMER_NUCLEAR_GUIDED:
        nuclei_mask, _ = run_mesmer_prediction(
            batch,
            mode="nuclei",
            image_mpp=float(params.get("image_mpp", params.get("pixel_size", 0.5))),
            batch_size=int(params.get("batch_size", 1) or 1),
            app=app,
        )
    mask = postprocess_mask(
        mask,
        min_size=int(params.get("postprocess_min_size", params.get("min_size", 0)) or 0),
        fill_holes=bool(params.get("fill_holes", False)),
        remove_border_objects=bool(params.get("remove_border_objects", False)),
    )
    out = {
        "mask": mask.astype(np.uint32, copy=False),
        "device_used": status.device_used,
        "runtime_seconds": runtime,
    }
    if nuclei_mask is not None:
        out["nuclei"] = nuclei_mask.astype(np.uint32, copy=False)
    return out


def run_mesmer_patch_preview(args, result_queue, stop_flag):
    """Queue-compatible Step1 Mesmer patch preview process."""
    try:
        loader = OMETIFFLoader(
            args["ome_path"],
            args.get("name_map") or {},
            correction_config=args.get("correction_config"),
        )
        loader.set_corrected_zarr_store(
            args.get("corrected_zarr_path"),
            args.get("corrected_decisions") or {},
        )
        fusion = FusionEngine()
        tasks = args["tasks"]
        groups = args.get("groups") or {}
        group_weights = args.get("group_weights") or {}
        nuc_ch = args.get("nuc_ch") or "DAPI"
        nuc_w = args.get("nuc_w", 1.0)
        output_dir = args.get("output_dir") or os.getcwd()
        preview_result_dir = os.path.join(output_dir, "patch_preview_results")
        os.makedirs(preview_result_dir, exist_ok=True)

        first_params = dict(tasks[0][2] if tasks else {})
        status = get_mesmer_device_status(first_params.get("use_gpu", "auto"))
        result_queue.put({
            "type": "progress",
            "done": 0,
            "total": len(tasks),
            "msg": (
                f"[Mesmer] deepcell available={status.deepcell_available} "
                f"tensorflow={status.tensorflow_version or 'n/a'} "
                f"device={status.device_used}"
            ),
        })
        if not status.mesmer_available:
            result_queue.put({"type": "error", "msg": status.error or "DeepCell/Mesmer is not installed in the current environment."})
            result_queue.put({"type": "finished"})
            return

        app = load_mesmer_application()
        total = len(tasks)
        for done, (patch_idx, roi, params) in enumerate(tasks):
            if stop_flag.is_set():
                break
            params = dict(params or {})
            method = params.get("method") or MESMER_WHOLE_CELL
            y0, y1, x0, x1 = [int(v) for v in roi]
            patch_id = f"P{patch_idx + 1}"
            started = time.perf_counter()
            result_queue.put({
                "type": "progress",
                "done": done,
                "total": total,
                "msg": f"[Mesmer] Patch {patch_id} prediction started",
            })
            try:
                membrane_channels = parse_channel_list(params.get("membrane_channels"))
                fusion_img = None
                input_mode = params.get("input_mode", "selected_channels")
                if str(input_mode).lower() in {"dapi + weighted fusion", "weighted_fusion", "step1_weighted_fusion"}:
                    fused = fusion.fuse_fullres(loader, y0, y1, x0, x1, groups, group_weights, nuc_ch, nuc_w)
                    fusion_img = np.asarray(fused[:, :, 0])
                    if not membrane_channels:
                        membrane_channels = [ch for ch, w in (params.get("step1_fusion_weights") or {}).items() if float(w or 0) > 0]
                batch = build_mesmer_input(
                    loader,
                    nuclear_channel=params.get("nuclear_channel") or nuc_ch,
                    membrane_channels=membrane_channels,
                    weights=params.get("channel_weights") or params.get("weights") or {},
                    bbox=(y0, y1, x0, x1),
                    normalize=bool(params.get("normalize_input", True)),
                    percentile_low=float(params.get("percentile_low", 1.0)),
                    percentile_high=float(params.get("percentile_high", 99.8)),
                    input_mode=input_mode,
                    fusion_image=fusion_img,
                )
                mask, pred_runtime = run_mesmer_prediction(
                    batch,
                    mode=method_to_mesmer_mode(method),
                    image_mpp=float(params.get("image_mpp", params.get("pixel_size", 0.5))),
                    batch_size=int(params.get("batch_size", 1) or 1),
                    app=app,
                )
                nuclei_mask = None
                if method == MESMER_NUCLEAR_GUIDED:
                    nuclei_mask, _ = run_mesmer_prediction(
                        batch,
                        mode="nuclei",
                        image_mpp=float(params.get("image_mpp", params.get("pixel_size", 0.5))),
                        batch_size=int(params.get("batch_size", 1) or 1),
                        app=app,
                    )
                mask = postprocess_mask(
                    mask,
                    min_size=int(params.get("postprocess_min_size", 0) or 0),
                    fill_holes=bool(params.get("fill_holes", False)),
                    remove_border_objects=bool(params.get("remove_border_objects", False)),
                )
                success = True
                err = ""
            except Exception:
                mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint32)
                nuclei_mask = None
                pred_runtime = 0.0
                success = False
                err = traceback.format_exc()
                result_queue.put({"type": "error", "msg": err})

            dapi = batch[0, :, :, 0] if "batch" in locals() else np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
            memb = batch[0, :, :, 1] if "batch" in locals() else np.zeros_like(dapi)
            rgb_raw = np.stack([
                (np.clip(memb, 0, 1) * 255).astype(np.uint8),
                np.zeros_like(dapi, dtype=np.uint8),
                (np.clip(dapi, 0, 1) * 255).astype(np.uint8),
            ], axis=-1)
            params["device_used"] = status.device_used
            params["tensorflow_version"] = status.tensorflow_version
            params["deepcell_version"] = status.deepcell_version
            params["runtime_seconds"] = time.perf_counter() - started
            result_path = os.path.join(preview_result_dir, f"{patch_id}_{method}_preview.npz")
            metadata = {
                "patch_id": patch_id,
                "patch_idx": int(patch_idx),
                "bbox_global": [y0, y1, x0, x1],
                "bbox_fullres": [y0, y1, x0, x1],
                "preview_image_shape": list(mask.shape),
                "params_used": params,
                "method": method,
                "phase": "preview",
                "labels_count": int(mask.max()) if mask.size else 0,
                "device_used": status.device_used,
                "runtime_seconds": params["runtime_seconds"],
                "success": success,
                "error": err,
            }
            np.savez_compressed(
                result_path,
                local_mask=mask.astype(np.uint32, copy=False),
                rgb_raw=rgb_raw,
                metadata=json.dumps(metadata, ensure_ascii=False),
            )
            result_queue.put({
                "type": "result",
                "patch_idx": patch_idx,
                "params": params,
                "rgb_overlay": rgb_raw,
                "rgb_raw": rgb_raw,
                "masks": mask.astype(np.uint32, copy=False),
                "result_path": result_path,
                "method": method,
                "patch_id": patch_id,
                "phase": 0,
                "bbox_global": [y0, y1, x0, x1],
                "bbox_fullres": [y0, y1, x0, x1],
                "labels_count": int(mask.max()) if mask.size else 0,
                "device_used": status.device_used,
                "runtime_seconds": params["runtime_seconds"],
                "success": success,
            })
            result_queue.put({
                "type": "progress",
                "done": done + 1,
                "total": total,
                "msg": f"[Mesmer] Patch {patch_id} labels={int(mask.max()) if mask.size else 0}",
            })
        result_queue.put({"type": "finished"})
    except Exception:
        result_queue.put({"type": "error", "msg": traceback.format_exc()})
        result_queue.put({"type": "finished"})


__all__ = [
    "MESMER_METHODS",
    "run_mesmer_patch_preview",
    "run_mesmer_on_fused_tile",
    "run_mesmer_on_channel_source",
    "run_mesmer_on_batch",
    "mesmer_metadata",
]
