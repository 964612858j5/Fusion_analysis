"""
block01/workers/cellpose_worker.py — Cellpose-related threads and overlay utilities.
"""

import os
import gc
import traceback
import numpy as np

from PyQt5.QtCore import QThread, pyqtSignal

from ..core.io_loader import OMETIFFLoader
from ..core.fusion_engine import FusionEngine
from ..utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_WHOLECELL_FUSION,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    normalize_segmentation_config,
)


class OverviewLoaderThread(QThread):
    """Background thread to load overview (DAPI full image downsampled)"""
    done  = pyqtSignal(object)   # float32 ndarray
    error = pyqtSignal(str)

    def __init__(self, loader, nuc_ch, downsample):
        super().__init__()
        self.loader     = loader
        self.nuc_ch     = nuc_ch
        self.downsample = downsample

    def run(self):
        try:
            arr = self.loader.read_region(
                self.nuc_ch,
                0, self.loader.shape[0],
                0, self.loader.shape[1],
                downsample=self.downsample,
            )
            self.done.emit(arr)
        except Exception as e:
            self.error.emit(str(e))


class PreviewLoaderThread(QThread):
    """Background thread to load all channels for one patch ROI.
    patch_idx is carried through all signals so multiple concurrent threads
    can be distinguished when their results arrive.
    """
    done     = pyqtSignal(int, dict)         # (patch_idx, {ch_name: ndarray})
    progress = pyqtSignal(int, int, int, str) # (patch_idx, done, total, ch_name)
    error    = pyqtSignal(int, str)           # (patch_idx, msg)

    def __init__(self, patch_idx, loader, channels, y0, y1, x0, x1, downsample):
        super().__init__()
        self.patch_idx  = patch_idx
        self.loader     = loader
        self.channels   = channels
        self.y0, self.y1, self.x0, self.x1 = y0, y1, x0, x1
        self.downsample = downsample
        self._stop      = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            cache = {}
            total = len(self.channels)
            for i, ch in enumerate(self.channels):
                if self._stop:
                    return
                self.progress.emit(self.patch_idx, i, total, ch)
                if ch in self.loader.ch_map:
                    cache[ch] = self.loader.read_region(
                        ch,
                        self.y0, self.y1,
                        self.x0, self.x1,
                        downsample=self.downsample,
                    )
            if not self._stop:
                self.done.emit(self.patch_idx, cache)
        except Exception as e:
            if not self._stop:
                self.error.emit(self.patch_idx, str(e))


# ══════════════════════════════════════════════════════════════════════
#  Cellpose-style mask overlay  (shared by grid search + Step 3 QC)
# ══════════════════════════════════════════════════════════════════════

def cellpose_mask_overlay(img_grey_u8, masks):
    """
    Cellpose-style mask overlay, fully vectorised.

    img_grey_u8 : uint8 2-D array (H, W) — single-channel DAPI / grey
    masks        : uint32 2-D array (H, W) — 0=background, 1..N=cell labels

    Returns uint8 RGB array (H, W, 3):
      • background → brightened grey  (S=0)
      • cells      → uniformly-spaced hues, S=1
                     brightness = max(img*1.5, 0.15) so cytoplasm regions
                     with weak DAPI signal remain visibly coloured
      • NO outlines / borders of any kind
    """
    n_cells = int(masks.max())

    V_base = np.clip(img_grey_u8.astype(np.float32) / 255.0 * 1.5, 0.0, 1.0)
    V = np.where(masks > 0, 1.0, V_base)

    H_lut = np.zeros(n_cells + 1, dtype=np.float32)
    S_lut = np.zeros(n_cells + 1, dtype=np.float32)
    if n_cells > 0:
        hues = np.linspace(0.0, 1.0, n_cells + 1)[
            np.random.permutation(n_cells)
        ]
        H_lut[1:] = hues
        S_lut[1:] = 1.0

    mask_idx = np.clip(masks, 0, n_cells).astype(np.int32)
    H = H_lut[mask_idx]
    S = S_lut[mask_idx]

    h6 = H * 6.0
    i  = h6.astype(np.int32) % 6
    f  = h6 - np.floor(h6)
    p  = V * (1.0 - S)
    q  = V * (1.0 - f * S)
    t  = V * (1.0 - (1.0 - f) * S)

    R = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [V,q,p,p,t,V])
    G = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [t,V,V,q,p,p])
    B = np.select([i==0, i==1, i==2, i==3, i==4, i==5], [p,p,t,V,V,q])

    RGB = np.stack([R, G, B], axis=-1)
    return (RGB * 255).astype(np.uint8)


def load_stardist_model(model_name, prefer_gpu=True, result_queue=None):
    """Load StarDist with TensorFlow/Keras backend, trying GPU then CPU."""
    os.environ["KERAS_BACKEND"] = "tensorflow"
    if result_queue is not None:
        result_queue.put({
            "type": "progress",
            "done": 0,
            "total": 1,
            "msg": "[Worker] KERAS_BACKEND=tensorflow",
        })

    def _emit(msg):
        print(msg)
        if result_queue is not None:
            result_queue.put({
                "type": "progress",
                "done": 0,
                "total": 1,
                "msg": msg,
            })

    if prefer_gpu:
        try:
            _emit("[Worker] trying StarDist GPU")
            import tensorflow as tf
            gpus = tf.config.list_physical_devices("GPU")
            if not gpus:
                raise RuntimeError("TensorFlow sees no GPU")
            for gpu in gpus:
                try:
                    tf.config.experimental.set_memory_growth(gpu, True)
                except Exception:
                    pass
            from csbdeep.utils import normalize as stardist_normalize
            from stardist.models import StarDist2D
            model = StarDist2D.from_pretrained(model_name)
            _emit("[Worker] StarDist device=gpu")
            return model, stardist_normalize, "gpu"
        except Exception as e:
            _emit(f"[Worker] GPU failed, fallback CPU: {e}")

    try:
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        import tensorflow as tf
        try:
            tf.config.set_visible_devices([], "GPU")
        except Exception:
            pass
        from csbdeep.utils import normalize as stardist_normalize
        from stardist.models import StarDist2D
        model = StarDist2D.from_pretrained(model_name)
        _emit("[Worker] StarDist device=cpu")
        return model, stardist_normalize, "cpu"
    except Exception:
        raise


def run_cellpose_process(args, result_queue, stop_flag):
    try:
        from cellpose import models
        import torch

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
        groups = args["groups"]
        group_weights = args["group_weights"]
        nuc_ch = args["nuc_ch"]
        nuc_w = args["nuc_w"]

        use_gpu = torch.cuda.is_available()
        device = torch.device("cuda" if use_gpu else "cpu")
        if use_gpu:
            free, total_vram = torch.cuda.mem_get_info(0)
            result_queue.put({
                "type": "progress",
                "done": 0,
                "total": len(tasks),
                "msg": (f"[Cellpose] GPU: {torch.cuda.get_device_name(0)}  "
                        f"VRAM free {free/1e9:.1f}/{total_vram/1e9:.1f} GB"),
            })
        else:
            result_queue.put({
                "type": "progress",
                "done": 0,
                "total": len(tasks),
                "msg": "[Cellpose] CUDA not available, using CPU (slower)",
            })

        cellpose_model = None
        stardist_model = None
        stardist_normalize = None
        stardist_device = None
        total = len(tasks)

        for done, (patch_idx, roi, params) in enumerate(tasks):
            if stop_flag.is_set():
                break

            params = normalize_segmentation_config(params)
            method = params.get("method", CELLPOSE_WHOLECELL_FUSION)
            print(f"[Worker] method={method}")
            y0, y1, x0, x1 = roi
            result_queue.put({
                "type": "progress",
                "done": done,
                "total": total,
                "msg": (f"Patch {patch_idx+1}  {method}  "
                        f"diam={params.get('diameter')}  "
                        f"flow={params.get('flow_threshold')}  "
                        f"prob={params.get('cellprob_threshold')}"),
            })

            fused = None
            fused_f32 = None
            dapi_f32 = None

            if method == CELLPOSE_WHOLECELL_FUSION:
                fused = fusion.fuse_fullres(
                    loader, y0, y1, x0, x1,
                    groups, group_weights, nuc_ch, nuc_w,
                )
                fused_f32 = np.array(fused.astype(np.float32) / 65535.0)
                seg_img = np.ascontiguousarray(fused_f32[:, :, 1])
                rgb_raw = np.stack([
                    (np.clip(fused_f32[:, :, 0], 0, 1) * 255).astype(np.uint8),
                    np.zeros_like(fused_f32[:, :, 0], dtype=np.uint8),
                    (np.clip(fused_f32[:, :, 1], 0, 1) * 255).astype(np.uint8),
                ], axis=-1)
            else:
                dapi_raw = loader.read_region(nuc_ch, y0, y1, x0, x1, downsample=1)
                dapi_f32 = fusion._normalize_intensity(dapi_raw).astype(np.float32)
                seg_img = np.ascontiguousarray(dapi_f32)
                dapi_u8 = (np.clip(dapi_f32, 0, 1) * 255).astype(np.uint8)
                rgb_raw = np.stack([
                    np.zeros_like(dapi_u8),
                    np.zeros_like(dapi_u8),
                    dapi_u8,
                ], axis=-1)

            try:
                if method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI):
                    if cellpose_model is None:
                        cellpose_model = models.CellposeModel(device=device)
                    masks_out, _, _ = cellpose_model.eval(
                        seg_img,
                        diameter=params.get("diameter"),
                        flow_threshold=params.get("flow_threshold", 0.4),
                        cellprob_threshold=params.get("cellprob_threshold", 0.0),
                        min_size=int(params.get("min_size", 15) or 15),
                        do_3D=False,
                    )
                elif method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION):
                    if stardist_model is None:
                        stardist_model, stardist_normalize, stardist_device = load_stardist_model(
                            params.get("model_name", "2D_versatile_fluo"),
                            prefer_gpu=params.get("device_preference", "gpu_first") != "cpu",
                            result_queue=result_queue,
                        )
                    img = stardist_normalize(seg_img, 1, 99.8, axis=(0, 1))
                    kwargs = {}
                    if params.get("prob_thresh") is not None:
                        kwargs["prob_thresh"] = params.get("prob_thresh")
                    if params.get("nms_thresh") is not None:
                        kwargs["nms_thresh"] = params.get("nms_thresh")
                    masks_out, _ = stardist_model.predict_instances(img, **kwargs)
                    if method == STARDIST_NUCLEI_EXPANSION:
                        from skimage.segmentation import expand_labels
                        dist = float(params.get("expand_distance", 8) or 0)
                        if dist > 0:
                            masks_out = expand_labels(masks_out, distance=dist)
                    if stardist_device:
                        params["device_used"] = stardist_device
                else:
                    raise ValueError(f"Unknown segmentation method: {method}")
                mask = masks_out.astype(np.uint32)
            except Exception as e:
                result_queue.put({"type": "error", "msg": traceback.format_exc()})
                mask = np.zeros(seg_img.shape, dtype=np.uint32)

            rgb_ov = rgb_raw
            n_cells = int(mask.max())
            mask_payload = mask.copy()

            del fused, fused_f32, dapi_f32, mask
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            result_queue.put({
                "type": "result",
                "patch_idx": patch_idx,
                "params": params,
                "rgb_overlay": rgb_ov,
                "rgb_raw": rgb_raw,
                "masks": mask_payload,
            })
            result_queue.put({
                "type": "progress",
                "done": done + 1,
                "total": total,
                "msg": f"✓ Patch {patch_idx+1}  cells={n_cells}",
            })

        del cellpose_model
        del stardist_model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        result_queue.put({"type": "finished"})

    except Exception:
        result_queue.put({"type": "error", "msg": traceback.format_exc()})
        result_queue.put({"type": "finished"})


# ══════════════════════════════════════════════════════════════════════
#  Cellpose Background Thread
# ══════════════════════════════════════════════════════════════════════

class CellposeWorker(QThread):
    # rgb_overlay is kept for legacy signal/API compatibility.
    # Viewers render masks from rgb_raw + masks through utils.mask_renderer.
    result_ready   = pyqtSignal(int, dict, object, object, object)  # patch_idx, params, rgb_overlay, rgb_raw, masks
    progress       = pyqtSignal(int, int, str)               # done, total, msg
    finished_all   = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, tasks, loader, fusion,
                 groups, group_weights, nuc_ch, nuc_w):
        super().__init__()
        self.tasks         = tasks   # [(patch_idx, (y0,y1,x0,x1), params), ...]
        self.loader        = loader
        self.fusion        = fusion
        self.groups        = groups
        self.group_weights = group_weights
        self.nuc_ch        = nuc_ch
        self.nuc_w         = nuc_w
        self._stop         = False

    @staticmethod
    def _make_dapi_rgb(dapi_f32):
        """Convert float32 [0,1] DAPI channel to uint8 2-D grey array."""
        return (np.clip(dapi_f32, 0, 1) * 255).astype(np.uint8)

    def stop(self):
        self._stop = True

    def run(self):
        try:
            from cellpose import models
            import torch

            use_gpu = torch.cuda.is_available()
            device  = torch.device("cuda" if use_gpu else "cpu")
            if use_gpu:
                free, total_vram = torch.cuda.mem_get_info(0)
                print(f"[Cellpose] GPU: {torch.cuda.get_device_name(0)}  "
                      f"VRAM free {free/1e9:.1f}/{total_vram/1e9:.1f} GB")
            else:
                print("[Cellpose] ⚠ CUDA not available, using CPU (slower)")
            # Cellpose 4.0.1+: model_type is ignored, always loads cpsam
            model = models.CellposeModel(device=device)
            total = len(self.tasks)

            for done, (patch_idx, roi, params) in enumerate(self.tasks):
                if self._stop:
                    break

                y0, y1, x0, x1 = roi
                self.progress.emit(
                    done, total,
                    f"Patch {patch_idx+1}  "
                    f"diam={params.get('diameter')}  "
                    f"flow={params.get('flow_threshold')}  "
                    f"prob={params.get('cellprob_threshold')}"
                )

                fused = self.fusion.fuse_fullres(
                    self.loader, y0, y1, x0, x1,
                    self.groups, self.group_weights,
                    self.nuc_ch, self.nuc_w,
                )

                print(f"[Cellpose] fused: shape={fused.shape} dtype={fused.dtype} "
                      f"min={fused.min()} max={fused.max()}")

                fused_f32 = np.array(fused.astype(np.float32) / 65535.0)

                print(f"[Cellpose] fused_f32: type={type(fused_f32)} "
                      f"shape={fused_f32.shape} "
                      f"ch0 range=[{fused_f32[:,:,0].min():.3f},{fused_f32[:,:,0].max():.3f}] "
                      f"ch1 range=[{fused_f32[:,:,1].min():.3f},{fused_f32[:,:,1].max():.3f}]")

                try:
                    nuc_2d = np.ascontiguousarray(fused_f32[:, :, 1])
                    print(f"[Cellpose] nuc_2d: type={type(nuc_2d)} shape={nuc_2d.shape} "
                          f"dtype={nuc_2d.dtype} contiguous={nuc_2d.flags['C_CONTIGUOUS']}")
                    masks_out, _, _ = model.eval(
                        nuc_2d,
                        diameter           = params.get("diameter"),
                        flow_threshold     = params.get("flow_threshold", 0.4),
                        cellprob_threshold = params.get("cellprob_threshold", 0.0),
                        min_size           = 15,
                        do_3D              = False,
                    )
                    mask = masks_out.astype(np.uint32)
                    print(f"[Cellpose] ✓ masks: shape={masks_out.shape} "
                          f"n_cells={mask.max()} unique={len(np.unique(mask))}")
                except Exception as e:
                    print(f"[Cellpose] ✗ model.eval failed: {e}")
                    import traceback as _tb
                    print(_tb.format_exc())
                    self.error_occurred.emit(str(e))
                    mask = np.zeros(fused.shape[:2], dtype=np.uint32)

                fusion_rgb = np.stack([
                    (np.clip(fused_f32[:, :, 0], 0, 1) * 255).astype(np.uint8),
                    np.zeros_like(fused_f32[:, :, 0], dtype=np.uint8),
                    (np.clip(fused_f32[:, :, 1], 0, 1) * 255).astype(np.uint8),
                ], axis=-1)
                dapi_u8  = fusion_rgb[:, :, 2]
                print(f"[Cellpose] dapi_u8: min={dapi_u8.min()} max={dapi_u8.max()} "
                      f"mean={dapi_u8.mean():.1f}")
                rgb_raw  = fusion_rgb
                rgb_ov   = rgb_raw
                n_cells  = int(mask.max())
                mask_payload = mask.copy()
                print(f"[Cellpose] rgb_raw shape={rgb_raw.shape}")

                del fused, fused_f32, mask, dapi_u8
                gc.collect()
                torch.cuda.empty_cache()

                self.result_ready.emit(patch_idx, params, rgb_ov, rgb_raw, mask_payload)
                self.progress.emit(done + 1, total,
                                   f"✓ Patch {patch_idx+1}  cells={n_cells}")

            del model
            gc.collect()
            torch.cuda.empty_cache()

        except Exception:
            self.error_occurred.emit(traceback.format_exc())

        self.finished_all.emit()
