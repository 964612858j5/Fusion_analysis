"""
block01/workers/cellpose_worker.py — Cellpose-related threads and overlay utilities.
"""

import gc
import traceback
import numpy as np

from PyQt5.QtCore import QThread, pyqtSignal

from ..core.io_loader import OMETIFFLoader
from ..core.fusion_engine import FusionEngine


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


def run_cellpose_process(args, result_queue, stop_flag):
    try:
        from cellpose import models
        import torch

        loader = OMETIFFLoader(
            args["ome_path"],
            args.get("name_map") or {},
            correction_config=args.get("correction_config"),
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

        model = models.CellposeModel(device=device)
        total = len(tasks)

        for done, (patch_idx, roi, params) in enumerate(tasks):
            if stop_flag.is_set():
                break

            y0, y1, x0, x1 = roi
            result_queue.put({
                "type": "progress",
                "done": done,
                "total": total,
                "msg": (f"Patch {patch_idx+1}  "
                        f"diam={params.get('diameter')}  "
                        f"flow={params.get('flow_threshold')}  "
                        f"prob={params.get('cellprob_threshold')}"),
            })

            fused = fusion.fuse_fullres(
                loader, y0, y1, x0, x1,
                groups, group_weights, nuc_ch, nuc_w,
            )
            fused_f32 = np.array(fused.astype(np.float32) / 65535.0)

            try:
                nuc_2d = np.ascontiguousarray(fused_f32[:, :, 1])
                masks_out, _, _ = model.eval(
                    nuc_2d,
                    diameter=params.get("diameter"),
                    flow_threshold=params.get("flow_threshold", 0.4),
                    cellprob_threshold=params.get("cellprob_threshold", 0.0),
                    min_size=15,
                    do_3D=False,
                )
                mask = masks_out.astype(np.uint32)
            except Exception as e:
                result_queue.put({"type": "error", "msg": str(e)})
                mask = np.zeros(fused.shape[:2], dtype=np.uint32)

            dapi_u8 = (np.clip(fused_f32[:, :, 1], 0, 1) * 255).astype(np.uint8)
            rgb_raw = np.stack([dapi_u8] * 3, axis=-1)
            rgb_ov = cellpose_mask_overlay(dapi_u8, mask)
            n_cells = int(mask.max())

            del fused, fused_f32, mask, dapi_u8
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            result_queue.put({
                "type": "result",
                "patch_idx": patch_idx,
                "params": params,
                "rgb_overlay": rgb_ov,
                "rgb_raw": rgb_raw,
            })
            result_queue.put({
                "type": "progress",
                "done": done + 1,
                "total": total,
                "msg": f"✓ Patch {patch_idx+1}  cells={n_cells}",
            })

        del model
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
    # rgb_overlay = DAPI grey + cellpose-style mask overlay (no borders)
    # rgb_raw     = DAPI grey only (no mask)
    result_ready   = pyqtSignal(int, dict, object, object)  # patch_idx, params, rgb_overlay, rgb_raw
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

                dapi_u8  = (np.clip(fused_f32[:, :, 1], 0, 1) * 255).astype(np.uint8)
                print(f"[Cellpose] dapi_u8: min={dapi_u8.min()} max={dapi_u8.max()} "
                      f"mean={dapi_u8.mean():.1f}")
                rgb_raw  = np.stack([dapi_u8]*3, axis=-1)
                rgb_ov   = cellpose_mask_overlay(dapi_u8, mask)
                n_cells  = int(mask.max())
                print(f"[Cellpose] rgb_ov shape={rgb_ov.shape} "
                      f"same_as_raw={np.array_equal(rgb_ov, rgb_raw)}")

                del fused, fused_f32, mask, dapi_u8
                gc.collect()
                torch.cuda.empty_cache()

                self.result_ready.emit(patch_idx, params, rgb_ov, rgb_raw)
                self.progress.emit(done + 1, total,
                                   f"✓ Patch {patch_idx+1}  cells={n_cells}")

            del model
            gc.collect()
            torch.cuda.empty_cache()

        except Exception:
            self.error_occurred.emit(traceback.format_exc())

        self.finished_all.emit()
