"""
block01/workers/segment_merge_worker.py — Tile-based segmentation + merge worker.
"""

import os
import gc
import json
import shutil
import traceback
import logging
import threading
from datetime import datetime

import numpy as np
import tifffile
import zarr

from PyQt5.QtCore import QThread, pyqtSignal

from ..utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_WHOLECELL_FUSION,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    normalize_segmentation_config,
)
from ..utils.segmentation_registry import (
    create_result_dir,
    register_legacy_result,
    upsert_result,
)


class SegmentMergeWorker(QThread):
    """
    Runs segmentation tile-by-tile on fused.zarr, then streams results into
    a global numpy memmap (no intermediate .npy files in normal mode).

    Tile ownership:
      Each tile is read with OVERLAP_PX padding on all sides.
      After inference, only cells whose centroid falls inside the tile's
      "own" region (without overlap) are kept.  This guarantees every
      cell is counted exactly once and no cell is truncated.

    Output:
      <project_output_dir>/segmentation_results/<timestamp>_<method>/...
    """

    tile_done  = pyqtSignal(int, int, int)   # tile_idx, n_tiles, n_cells_this_tile
    progress   = pyqtSignal(int, int, str)   # done, total, message
    finished   = pyqtSignal(str, int)        # output_dir, total_cells
    error      = pyqtSignal(str)

    def __init__(self, zarr_path, seg_config=None, n_rows=1, n_cols=1,
                 overlap_px=200, output_dir=None, recovery_npy_dir=None,
                 rois=None, cp_params=None):
        super().__init__()
        self.zarr_path        = zarr_path
        self.seg_config       = normalize_segmentation_config(seg_config if seg_config is not None else cp_params)
        self.method           = self.seg_config.get("method", CELLPOSE_WHOLECELL_FUSION)
        self.n_rows           = n_rows
        self.n_cols           = n_cols
        self.overlap_px       = overlap_px
        self.project_output_dir = os.path.abspath(output_dir or os.getcwd())
        self.result_id, self.output_dir, self.created_at = create_result_dir(
            self.project_output_dir, self.method
        )
        self.recovery_npy_dir = recovery_npy_dir
        self.rois             = rois
        self._stop            = False
        self._logger          = None
        self._mem_timer       = None
        self._last_region_meta = None

    @staticmethod
    def _abs(path):
        return os.path.abspath(path) if path else path

    def _project_path(self, *parts):
        return os.path.join(self.project_output_dir, *parts)

    def _write_segmentation_config(self):
        path = os.path.join(self.output_dir, "segmentation_config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.seg_config, f, indent=2)
        return path

    def _multichannel_source_path(self):
        candidates = [
            self._project_path("corrected_channels.zarr"),
            os.path.join(os.path.dirname(self.project_output_dir), "corrected_channels.zarr"),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return self._abs(path)
        return ""

    def _register_completed_result(self, summary_meta):
        config_path = os.path.join(self.output_dir, "segmentation_config.json")
        meta_path = os.path.join(self.output_dir, "segmentation_meta.json")
        method = self.seg_config.get("method", self.method)
        display_name = self.seg_config.get("display_name", method)

        mask_path = summary_meta.get("ome_tiff") or summary_meta.get("mask_path") or ""
        dapi_path = summary_meta.get("global_dapi") or summary_meta.get("dapi_path") or ""
        fusion_path = (
            summary_meta.get("fused_zarr_path")
            or summary_meta.get("input_zarr")
            or summary_meta.get("source_zarr")
            or self.zarr_path
        )
        rois = summary_meta.get("rois") or []
        if rois:
            first = rois[0]
            mask_path = first.get("ome_tiff") or first.get("mask_path") or mask_path
            dapi_path = first.get("global_dapi") or first.get("dapi_path") or dapi_path
            fusion_path = first.get("fused_zarr_path") or first.get("input_zarr") or fusion_path

        entry = {
            "result_id": self.result_id,
            "method": method,
            "display_name": display_name,
            "created_at": self.created_at,
            "status": "completed",
            "mask_path": self._abs(mask_path) if mask_path else "",
            "dapi_path": self._abs(dapi_path) if dapi_path else "",
            "fusion_path": self._abs(fusion_path) if fusion_path else "",
            "multichannel_source_path": self._multichannel_source_path(),
            "config_path": self._abs(config_path),
            "meta_path": self._abs(meta_path),
            "output_dir": self._abs(self.output_dir),
            "notes": "",
        }
        upsert_result(self.project_output_dir, entry)
        return entry

    def _make_alias(self, source_path, alias_name):
        if not source_path or not os.path.exists(source_path):
            return ""
        dst = os.path.join(self.output_dir, alias_name)
        if os.path.lexists(dst):
            return self._abs(dst)
        try:
            rel = os.path.relpath(source_path, os.path.dirname(dst))
            os.symlink(rel, dst, target_is_directory=os.path.isdir(source_path))
        except Exception:
            if os.path.isdir(source_path):
                shutil.copytree(source_path, dst)
            else:
                shutil.copy2(source_path, dst)
        return self._abs(dst)

    def _write_roi_canonical_aliases(self, roi_meta_all):
        """Expose first ROI result under standard names for legacy readers."""
        if not roi_meta_all:
            return
        first = roi_meta_all[0]
        mask_alias = self._make_alias(first.get("ome_tiff") or first.get("mask_path"), "global_mask.ome.tiff")
        dapi_alias = self._make_alias(first.get("global_dapi") or first.get("dapi_path"), "global_dapi.ome.tiff")
        zarr_alias = self._make_alias(first.get("zarr_path"), "global_mask.zarr")
        if mask_alias:
            first["mask_path"] = mask_alias
            first["ome_tiff"] = mask_alias
        if dapi_alias:
            first["dapi_path"] = dapi_alias
            first["global_dapi"] = dapi_alias
        if zarr_alias:
            first["zarr_path"] = zarr_alias

    def stop(self):
        self._stop = True

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _drop_caches():
        """
        Tell the Linux kernel to drop page cache, dentries and inodes.
        Falls back silently if not root or not Linux.
        """
        try:
            os.system('sync')
            with open('/proc/sys/vm/drop_caches', 'w') as f:
                f.write('3\n')
        except Exception:
            pass

    # ── logging helpers ──────────────────────────────────────────────

    def _setup_logger(self):
        """
        Create a per-run log file:
          <output_dir>/segmentation_<YYYYMMDD_HHMMSS>.log
        Logs to file (DEBUG) and stdout (INFO).
        """
        os.makedirs(self.output_dir, exist_ok=True)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(self.output_dir, f"segmentation_{ts}.log")

        logger = logging.getLogger(f"seg_{ts}")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        logger.info(f"Log file: {log_path}")
        logger.info(f"zarr: {self.zarr_path}")
        logger.info(f"project output_dir: {self.project_output_dir}")
        logger.info(f"result_id: {self.result_id}")
        logger.info(f"result output_dir: {self.output_dir}")
        logger.info(f"Grid: {self.n_rows}×{self.n_cols}  overlap={self.overlap_px}px")
        logger.info(f"Segmentation config: {self.seg_config}")
        logger.info(f"[Step2] segmentation method={self.seg_config.get('method')}")
        logger.info(f"[Step2] input_type={self.seg_config.get('input_type')}")
        return logger, log_path

    @staticmethod
    def _mem_snapshot():
        """Return a formatted string with current RAM and VRAM usage."""
        parts = []
        try:
            import psutil
            m   = psutil.virtual_memory()
            used = (m.total - m.available) / 1e9
            tot  = m.total / 1e9
            parts.append(f"RAM {used:.1f}/{tot:.1f}GB ({m.percent:.0f}%)")
        except ImportError:
            parts.append("RAM (psutil not installed)")

        try:
            import torch
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    alloc  = torch.cuda.memory_allocated(i)  / 1e9
                    reserv = torch.cuda.memory_reserved(i)   / 1e9
                    total  = torch.cuda.get_device_properties(i).total_memory / 1e9
                    parts.append(
                        f"GPU{i} alloc={alloc:.1f}GB "
                        f"reserved={reserv:.1f}GB "
                        f"total={total:.1f}GB"
                    )
        except Exception:
            pass

        return "  |  ".join(parts)

    def _start_mem_logger(self, interval_s=10):
        """Start a background daemon thread that logs RAM + VRAM every interval_s seconds."""
        self._mem_log_active = True

        def _loop():
            while self._mem_log_active and not self._stop:
                if self._logger:
                    self._logger.debug(f"[MEM] {self._mem_snapshot()}")
                import time as _t
                _t.sleep(interval_s)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        self._mem_timer = t

    def _stop_mem_logger(self):
        self._mem_log_active = False

    def _read_dapi_from_zarr(self, z, y0, y1, x0, x1):
        """
        Read the nucleus (DAPI) channel directly from fused zarr channel index 1.
        fused zarr shape: (H, W, 2)  ch0=cyto  ch1=nucleus(DAPI)  dtype=uint16
        Returns uint16 ndarray (H, W).
        """
        return np.array(z[y0:y1, x0:x1, 1])

    @staticmethod
    def _normalize01(arr):
        arr = np.asarray(arr, dtype=np.float32)
        nz = arr[arr > 0]
        if nz.size > 100:
            lo, hi = np.percentile(nz, [1.0, 99.8])
            if hi > lo:
                return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
        vmax = float(arr.max()) if arr.size else 0.0
        if vmax > 0:
            return np.clip(arr / vmax, 0.0, 1.0)
        return np.zeros_like(arr, dtype=np.float32)

    def _init_segmentation_backend(self, use_gpu, device):
        method = self.seg_config.get("method", CELLPOSE_WHOLECELL_FUSION)
        if method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI):
            from cellpose import models as cp_models
            return {"cellpose": cp_models.CellposeModel(device=device)}
        if method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION):
            from stardist.models import StarDist2D
            model_name = self.seg_config.get("model_name", "2D_versatile_fluo")
            return {"stardist": StarDist2D.from_pretrained(model_name)}
        raise ValueError(f"Unknown segmentation method: {method}")

    def _segment_tile(self, tile_data, backend):
        """Return a uint32 label mask for one read tile."""
        method = self.seg_config.get("method", CELLPOSE_WHOLECELL_FUSION)
        tile_f32 = tile_data.astype(np.float32) / 65535.0

        if method == CELLPOSE_WHOLECELL_FUSION:
            masks, _, _ = backend["cellpose"].eval(
                tile_f32,
                diameter=self.seg_config.get("diameter"),
                flow_threshold=self.seg_config.get("flow_threshold", 0.4),
                cellprob_threshold=self.seg_config.get("cellprob_threshold", 0.0),
                min_size=self.seg_config.get("min_size", 15),
                do_3D=False,
            )
            return masks.astype(np.uint32)

        dapi = np.ascontiguousarray(tile_f32[:, :, 1])
        if method == CELLPOSE_NUCLEI_DAPI:
            masks, _, _ = backend["cellpose"].eval(
                dapi,
                diameter=self.seg_config.get("diameter"),
                flow_threshold=self.seg_config.get("flow_threshold", 0.4),
                cellprob_threshold=self.seg_config.get("cellprob_threshold", 0.0),
                min_size=self.seg_config.get("min_size", 15),
                do_3D=False,
            )
            return masks.astype(np.uint32)

        if method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION):
            from csbdeep.utils import normalize as stardist_normalize
            img = stardist_normalize(dapi, 1, 99.8, axis=(0, 1))
            kwargs = {}
            if self.seg_config.get("prob_thresh") is not None:
                kwargs["prob_thresh"] = self.seg_config.get("prob_thresh")
            if self.seg_config.get("nms_thresh") is not None:
                kwargs["nms_thresh"] = self.seg_config.get("nms_thresh")
            masks, _ = backend["stardist"].predict_instances(img, **kwargs)
            if method == STARDIST_NUCLEI_EXPANSION:
                from skimage.segmentation import expand_labels
                dist = float(self.seg_config.get("expand_distance", 8) or 0)
                if dist > 0:
                    masks = expand_labels(masks, distance=dist)
            return masks.astype(np.uint32)

        raise ValueError(f"Unknown segmentation method: {method}")

    @staticmethod
    def _write_tile_ometiff(path, arr, description=""):
        """Write a 2-D array as a tiled OME-TIFF (single IFD, LZW, 512×512 tiles)."""
        with tifffile.TiffWriter(path, bigtiff=True) as tif:
            tif.write(
                arr,
                tile=(512, 512),
                compression='lzw',
                photometric='minisblack',
                metadata=None,
                description=description,
            )

    def _segment_one_zarr(self, zarr_path, out_prefix,
                          model, use_gpu, log,
                          poly_fullres=None, bbox=None):
        """
        Segment one zarr file (one ROI or full WSI).

        Per-tile outputs (inside <output_dir>/tile_masks/<out_prefix>/):
          tile_r{r}_c{c}_dapi.ome.tiff      — DAPI uint16 (own region, no overlap)
          tile_r{r}_c{c}_raw_mask.ome.tiff  — raw segmentation mask float32 (own region, no overlap)

        Global outputs:
          global_mask_<out_prefix>.dat       — memmap uint32
          global_mask_<out_prefix>.zarr      — zarr uint32
          global_mask_<out_prefix>.ome.tiff  — merged mask float32 (QuPath-compatible)
          global_dapi_<out_prefix>.ome.tiff  — full-region DAPI uint16 (tiled)

        Returns total cell count.
        """
        import torch
        z      = zarr.open(zarr_path, mode='r')
        full_h = z.shape[0]
        full_w = z.shape[1]
        log.info(f"  zarr: {full_h}×{full_w} px")

        tile_h = -(-full_h // self.n_rows)
        tile_w = -(-full_w // self.n_cols)

        tiles = []
        for r in range(self.n_rows):
            for c in range(self.n_cols):
                oy0 = r * tile_h
                oy1 = min(oy0 + tile_h, full_h)
                ox0 = c * tile_w
                ox1 = min(ox0 + tile_w, full_w)
                ry0 = max(0, oy0 - self.overlap_px)
                ry1 = min(full_h, oy1 + self.overlap_px)
                rx0 = max(0, ox0 - self.overlap_px)
                rx1 = min(full_w, ox1 + self.overlap_px)
                tiles.append({
                    'row': r, 'col': c,
                    'own':  (oy0, oy1, ox0, ox1),
                    'read': (ry0, ry1, rx0, rx1),
                })
        n_tiles = len(tiles)

        mmap_path = os.path.join(
            self.output_dir, f'global_mask_{out_prefix}.dat'
        )
        mmap = np.memmap(mmap_path, dtype='uint32', mode='w+',
                         shape=(full_h, full_w))
        mmap[:] = 0

        tile_dir = os.path.join(self.output_dir, 'tile_masks', out_prefix)
        os.makedirs(tile_dir, exist_ok=True)

        dapi_mmap_path = os.path.join(
            self.output_dir, f'global_dapi_{out_prefix}.dat'
        )
        dapi_mmap = np.memmap(dapi_mmap_path, dtype='uint16', mode='w+',
                              shape=(full_h, full_w))
        dapi_mmap[:] = 0

        global_id_offset = 0
        tile_stats = []

        for i, tile in enumerate(tiles):
            if self._stop:
                del mmap, dapi_mmap
                return 0

            row, col           = tile['row'], tile['col']
            oy0, oy1, ox0, ox1 = tile['own']
            ry0, ry1, rx0, rx1 = tile['read']
            own_h = oy1 - oy0
            own_w = ox1 - ox0

            log.info(
                f"  [{out_prefix}] Tile [{i+1}/{n_tiles}] "
                f"row={row} col={col}"
            )
            self.progress.emit(
                i, n_tiles,
                f"[{out_prefix}] Tile [{i+1}/{n_tiles}]  "
                f"({ry1-ry0}×{rx1-rx0}px)"
            )

            tile_data = np.array(z[ry0:ry1, rx0:rx1, :])

            dapi_own = self._read_dapi_from_zarr(z, oy0, oy1, ox0, ox1)
            dapi_mmap[oy0:oy1, ox0:ox1] = dapi_own[:own_h, :own_w]

            if self.recovery_npy_dir is not None:
                npy_path = os.path.join(
                    self.recovery_npy_dir,
                    f'tile_{out_prefix}_{row}_{col}.npy'
                )
                if not os.path.exists(npy_path):
                    log.warning(f"  Missing: {npy_path}, skipping")
                    del tile_data
                    continue
                local_mask = np.load(npy_path)
            else:
                try:
                    local_mask = self._segment_tile(tile_data, model)
                except Exception as e:
                    log.error(f"  Tile [{row},{col}] failed: {traceback.format_exc()}")
                    local_mask = np.zeros((ry1-ry0, rx1-rx0), dtype=np.uint32)
                if use_gpu:
                    torch.cuda.empty_cache()

            del tile_data

            if self._stop:
                del local_mask, dapi_own
                mmap.flush()
                dapi_mmap.flush()
                del mmap, dapi_mmap
                log.info(f"  [{out_prefix}] Stopped by user after tile [{i+1}/{n_tiles}].")
                return 0

            local_oy0 = oy0 - ry0
            local_oy1 = oy1 - ry0
            local_ox0 = ox0 - rx0
            local_ox1 = ox1 - rx0
            raw_own_mask = local_mask[local_oy0:local_oy1,
                                      local_ox0:local_ox1].copy()

            dapi_tile_path = os.path.join(
                tile_dir, f'tile_r{row}_c{col}_dapi.ome.tiff'
            )
            try:
                self._write_tile_ometiff(
                    dapi_tile_path,
                    dapi_own[:own_h, :own_w].astype(np.uint16),
                    description=f'DAPI  row={row} col={col}  '
                                f'own=({oy0},{oy1},{ox0},{ox1})',
                )
                log.info(f"    dapi tile → {dapi_tile_path}")
            except Exception as e:
                log.warning(f"    dapi tile write failed: {e}")

            raw_mask_tile_path = os.path.join(
                tile_dir, f'tile_r{row}_c{col}_raw_mask.ome.tiff'
            )
            try:
                self._write_tile_ometiff(
                    raw_mask_tile_path,
                    raw_own_mask.astype(np.float32),
                    description=f'raw segmentation mask  row={row} col={col}  '
                                f'n_cells={int(raw_own_mask.max())}',
                )
                log.info(f"    raw mask tile → {raw_mask_tile_path}")
            except Exception as e:
                log.warning(f"    raw mask tile write failed: {e}")
            del raw_own_mask

            n_raw = int(local_mask.max())
            if n_raw == 0:
                self.tile_done.emit(i, n_tiles, 0)
                del local_mask
                gc.collect()
                self._drop_caches()
                continue

            cy, cx = self._centroids_vectorised(local_mask)

            keep_labels = []
            for label_idx in range(n_raw):
                lcy, lcx = cy[label_idx], cx[label_idx]
                if (lcy >= local_oy0 and lcy < local_oy1 and
                        lcx >= local_ox0 and lcx < local_ox1):
                    keep_labels.append(label_idx + 1)

            if not keep_labels:
                self.tile_done.emit(i, n_tiles, 0)
                del local_mask, cy, cx
                gc.collect()
                self._drop_caches()
                continue

            lut = np.zeros(n_raw + 1, dtype=np.uint32)
            for new_id, lab in enumerate(keep_labels, start=1):
                lut[lab] = new_id + global_id_offset

            remapped = lut[local_mask]
            del local_mask, lut, cy, cx

            dst = mmap[ry0:ry1, rx0:rx1]
            np.copyto(dst, remapped, where=(remapped > 0))
            del remapped

            n_kept = len(keep_labels)
            global_id_offset += n_kept
            tile_stats.append({
                'row': row,
                'col': col,
                'n_cells': n_kept,
                'bbox_local': [oy0, oy1, ox0, ox1],
                'dapi_path': self._abs(dapi_tile_path),
                'mask_path': self._abs(raw_mask_tile_path),
            })

            self.tile_done.emit(i, n_tiles, n_kept)
            log.info(f"  ✓ [{out_prefix}] Tile [{i+1}/{n_tiles}] kept={n_kept}")
            gc.collect()
            self._drop_caches()

        # ── Flush memmaps ─────────────────────────────────────────────
        mmap.flush()
        dapi_mmap.flush()
        total_cells = int(global_id_offset)
        log.info(f"  [{out_prefix}] total_cells={total_cells:,}")

        del mmap, dapi_mmap
        gc.collect()
        self._drop_caches()

        if self._stop:
            log.info(f"  [{out_prefix}] Stopped by user — skipping TIFF/zarr output.")
            return 0

        CHUNK = 4096
        mmap_ro      = np.memmap(mmap_path,      dtype='uint32', mode='r',
                                 shape=(full_h, full_w))
        dapi_mmap_ro = np.memmap(dapi_mmap_path, dtype='uint16', mode='r',
                                 shape=(full_h, full_w))

        out_zarr_path = os.path.join(
            self.output_dir, f'global_mask_{out_prefix}.zarr'
        )
        out_z = zarr.open(
            out_zarr_path, mode='w',
            shape=(full_h, full_w), dtype='uint32',
            chunks=(1024, 1024),
        )
        for y in range(0, full_h, CHUNK):
            if self._stop:
                log.info(f"  [{out_prefix}] Stopped during zarr write.")
                del mmap_ro, dapi_mmap_ro
                return 0
            out_z[y:y+CHUNK, :] = mmap_ro[y:y+CHUNK, :]
        self._drop_caches()

        if self._stop:
            del mmap_ro, dapi_mmap_ro
            return 0
        ome_path = os.path.join(
            self.output_dir, f'global_mask_{out_prefix}.ome.tiff'
        )
        with tifffile.TiffWriter(ome_path, bigtiff=True) as tif:
            tif.write(
                mmap_ro.astype(np.float32),
                tile=(512, 512),
                compression='lzw',
                photometric='minisblack',
                metadata=None,
            )
        self._drop_caches()

        if self._stop:
            del mmap_ro, dapi_mmap_ro
            return 0
        global_dapi_path = os.path.join(
            self.output_dir, f'global_dapi_{out_prefix}.ome.tiff'
        )
        with tifffile.TiffWriter(global_dapi_path, bigtiff=True) as tif:
            tif.write(
                np.array(dapi_mmap_ro),
                tile=(512, 512),
                compression='lzw',
                photometric='minisblack',
                metadata=None,
            )
        self._drop_caches()

        del mmap_ro, dapi_mmap_ro
        gc.collect()

        meta = {
            'mode':            'roi',
            'roi_name':        out_prefix,
            'zarr_path':       self._abs(out_zarr_path),
            'fused_zarr_path':  self._abs(zarr_path),
            'input_zarr':       self._abs(zarr_path),
            'source_zarr':      self._abs(zarr_path),
            'ome_tiff':        self._abs(ome_path),
            'mask_path':        self._abs(ome_path),
            'global_dapi':     self._abs(global_dapi_path),
            'dapi_path':        self._abs(global_dapi_path),
            'tile_dir':        self._abs(tile_dir),
            'tiles_dir':        self._abs(tile_dir),
            'mmap_path':       self._abs(mmap_path),
            'total_cells':     total_cells,
            'image_shape':      [full_h, full_w],
            'tile_grid':        [self.n_rows, self.n_cols],
            'tile_stats':      tile_stats,
            'seg_config':      self.seg_config,
            'cp_params':       self.seg_config,
            'bbox':            list(bbox) if bbox else None,
            'created_at':      datetime.now().isoformat(),
        }
        with open(
            os.path.join(self.output_dir,
                         f'segmentation_meta_{out_prefix}.json'), 'w'
        ) as f:
            json.dump(meta, f, indent=2)

        self._last_region_meta = meta
        log.info(f"  [{out_prefix}] outputs written")
        return total_cells

    @staticmethod
    def _centroids_vectorised(mask):
        """
        Return arrays cy, cx for each label 1..max_label.
        Labels not present get cy=cx=-1.
        Uses bincount (fast, no Python loops).
        """
        n = int(mask.max())
        if n == 0:
            return np.array([]), np.array([])
        h, w  = mask.shape
        flat  = mask.ravel()
        ys    = np.repeat(np.arange(h, dtype=np.float32), w)
        xs    = np.tile  (np.arange(w, dtype=np.float32), h)
        cnts  = np.bincount(flat, minlength=n + 2)
        sum_y = np.bincount(flat, weights=ys, minlength=n + 2)
        sum_x = np.bincount(flat, weights=xs, minlength=n + 2)
        valid = cnts[1:n+1] > 0
        cy = np.where(valid, sum_y[1:n+1] / np.maximum(cnts[1:n+1], 1), -1)
        cx = np.where(valid, sum_x[1:n+1] / np.maximum(cnts[1:n+1], 1), -1)
        return cy, cx   # length n, index i → label i+1

    # ── main run ──────────────────────────────────────────────────────

    def run(self):
        try:
            import torch

            self._logger, log_path = self._setup_logger()
            log = self._logger
            log.info("=== Segmentation started ===")
            register_legacy_result(self.project_output_dir)
            config_path = self._write_segmentation_config()
            log.info(f"segmentation_config.json → {config_path}")

            try:
                import psutil
            except ImportError:
                log.warning(
                    "psutil not installed — RAM usage cannot be monitored. "
                    "Run: pip install psutil"
                )
                print("[WARNING] pip install psutil  — RAM monitoring disabled")

            log.info(f"Initial memory: {self._mem_snapshot()}")
            self._start_mem_logger(interval_s=10)

            if self.recovery_npy_dir is None:
                use_gpu = torch.cuda.is_available()
                device  = torch.device('cuda' if use_gpu else 'cpu')
                model   = self._init_segmentation_backend(use_gpu, device)
            else:
                model   = None
                use_gpu = False

            # ── ROI mode ─────────────────────────────────────────────
            if self.rois:
                log.info(f"ROI mode: {len(self.rois)} ROI(s)")
                total_cells_all = 0
                roi_meta_all = []
                for roi_i, roi in enumerate(self.rois):
                    if self._stop:
                        break
                    roi_name = roi["name"]
                    roi_zarr = os.path.join(
                        self.project_output_dir, f"fused_{roi_name}.zarr"
                    )
                    if not os.path.exists(roi_zarr):
                        log.warning(f"ROI zarr not found: {roi_zarr} — skipping")
                        continue
                    roi_zarr_abs = self._abs(roi_zarr)
                    log.info(
                        f"=== ROI [{roi_i+1}/{len(self.rois)}]: {roi_name} ==="
                    )
                    self.progress.emit(
                        roi_i, len(self.rois),
                        f"Segmenting ROI [{roi_i+1}/{len(self.rois)}]: {roi_name}…"
                    )
                    n_cells = self._segment_one_zarr(
                        zarr_path    = roi_zarr_abs,
                        out_prefix   = roi_name,
                        model        = model,
                        use_gpu      = use_gpu,
                        log          = log,
                        poly_fullres = roi.get("polygon_fullres"),
                        bbox         = roi.get("bbox_fullres"),
                    )
                    region_meta = self._last_region_meta or {}
                    total_cells_all += n_cells
                    roi_meta_all.append({
                        "roi_name": roi_name,
                        "bbox_fullres": roi.get("bbox_fullres"),
                        "fused_zarr_path": roi_zarr_abs,
                        "input_zarr": roi_zarr_abs,
                        "source_zarr": roi_zarr_abs,
                        "mask_path": region_meta.get("mask_path") or region_meta.get("ome_tiff"),
                        "ome_tiff": region_meta.get("ome_tiff"),
                        "dapi_path": region_meta.get("dapi_path") or region_meta.get("global_dapi"),
                        "global_dapi": region_meta.get("global_dapi"),
                        "zarr_path": region_meta.get("zarr_path"),
                        "tiles_dir": region_meta.get("tiles_dir") or self._abs(os.path.join(self.output_dir, "tile_masks", roi_name)),
                        "tile_grid": [self.n_rows, self.n_cols],
                        "total_cells": n_cells,
                        "seg_config": self.seg_config,
                    })
                    self.progress.emit(
                        roi_i + 1, len(self.rois),
                        f"✓ ROI {roi_name}: {n_cells:,} cells  "
                        f"(cumulative: {total_cells_all:,})"
                    )

                if model is not None:
                    del model
                    gc.collect()
                    if use_gpu and torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    self._drop_caches()

                log.info(
                    f"=== All ROIs done  total_cells={total_cells_all:,} ==="
                )
                self._write_roi_canonical_aliases(roi_meta_all)
                summary_meta = {
                    "mode": "roi",
                    "output_dir": self._abs(self.output_dir),
                    "project_output_dir": self._abs(self.project_output_dir),
                    "result_id": self.result_id,
                    "method": self.method,
                    "display_name": self.seg_config.get("display_name", self.method),
                    "rois": roi_meta_all,
                    "total_cells": total_cells_all,
                    "seg_config": self.seg_config,
                    "config_path": self._abs(config_path),
                    "created_at": datetime.now().isoformat(),
                }
                with open(os.path.join(self.output_dir, "segmentation_meta.json"), "w") as f:
                    json.dump(summary_meta, f, indent=2)
                self._register_completed_result(summary_meta)
                self._stop_mem_logger()
                self.finished.emit(self.output_dir, total_cells_all)
                return

            # ── Full WSI mode ─────────────────────────────────────────
            z       = zarr.open(self.zarr_path, mode='r')
            full_h  = z.shape[0]
            full_w  = z.shape[1]
            log.info(f"Input zarr: {full_h}×{full_w} px")

            tile_h = -(-full_h // self.n_rows)
            tile_w = -(-full_w // self.n_cols)

            tiles = []
            for r in range(self.n_rows):
                for c in range(self.n_cols):
                    oy0 = r * tile_h
                    oy1 = min(oy0 + tile_h, full_h)
                    ox0 = c * tile_w
                    ox1 = min(ox0 + tile_w, full_w)
                    ry0 = max(0, oy0 - self.overlap_px)
                    ry1 = min(full_h, oy1 + self.overlap_px)
                    rx0 = max(0, ox0 - self.overlap_px)
                    rx1 = min(full_w, ox1 + self.overlap_px)
                    tiles.append({
                        'row': r, 'col': c,
                        'own':  (oy0, oy1, ox0, ox1),
                        'read': (ry0, ry1, rx0, rx1),
                    })
            n_tiles = len(tiles)

            os.makedirs(self.output_dir, exist_ok=True)

            tile_dir = os.path.join(self.output_dir, 'tile_masks')
            os.makedirs(tile_dir, exist_ok=True)

            mmap_path = os.path.join(self.output_dir, 'global_mask.dat')
            mmap = np.memmap(mmap_path, dtype='uint32', mode='w+',
                             shape=(full_h, full_w))
            mmap[:] = 0

            dapi_mmap_path = os.path.join(self.output_dir, 'global_dapi.dat')
            dapi_mmap = np.memmap(dapi_mmap_path, dtype='uint16', mode='w+',
                                  shape=(full_h, full_w))
            dapi_mmap[:] = 0

            global_id_offset = 0
            tile_stats = []

            for i, tile in enumerate(tiles):
                if self._stop:
                    self.error.emit('Stopped by user.')
                    return

                row, col            = tile['row'], tile['col']
                oy0, oy1, ox0, ox1  = tile['own']
                ry0, ry1, rx0, rx1  = tile['read']
                own_h = oy1 - oy0
                own_w = ox1 - ox0

                _msg = (
                    f"Tile [{i+1}/{n_tiles}]  row={row} col={col}  "
                    f"read=({ry1-ry0}×{rx1-rx0}px)  own=({own_h}×{own_w}px)"
                )
                self.progress.emit(i, n_tiles, _msg)
                log.info(_msg)
                log.debug(f"  [MEM before inference] {self._mem_snapshot()}")

                dapi_own = self._read_dapi_from_zarr(z, oy0, oy1, ox0, ox1)
                dapi_mmap[oy0:oy1, ox0:ox1] = dapi_own[:own_h, :own_w]

                if self.recovery_npy_dir is not None:
                    npy_path = os.path.join(
                        self.recovery_npy_dir, f'tile_{row}_{col}.npy'
                    )
                    if not os.path.exists(npy_path):
                        self.progress.emit(
                            i, n_tiles,
                            f'  ⚠ {npy_path} not found, skipping'
                        )
                        continue
                    local_mask = np.load(npy_path)
                else:
                    tile_data = np.array(z[ry0:ry1, rx0:rx1, :])
                    try:
                        local_mask = self._segment_tile(tile_data, model)
                    except Exception as e:
                        log.error(f'Tile [{row},{col}] inference failed:\n{traceback.format_exc()}')
                        self.error.emit(f'Tile [{row},{col}] inference failed: {e}')
                        local_mask = np.zeros((ry1-ry0, rx1-rx0), dtype=np.uint32)
                    del tile_data
                    if use_gpu:
                        torch.cuda.empty_cache()

                local_oy0 = oy0 - ry0
                local_oy1 = oy1 - ry0
                local_ox0 = ox0 - rx0
                local_ox1 = ox1 - rx0
                raw_own_mask = local_mask[local_oy0:local_oy1,
                                          local_ox0:local_ox1].copy()

                dapi_tile_path = os.path.join(
                    tile_dir, f'tile_r{row}_c{col}_dapi.ome.tiff'
                )
                try:
                    self._write_tile_ometiff(
                        dapi_tile_path,
                        dapi_own[:own_h, :own_w].astype(np.uint16),
                        description=f'DAPI row={row} col={col} '
                                    f'own=({oy0},{oy1},{ox0},{ox1})',
                    )
                except Exception as e:
                    log.warning(f"  dapi tile write failed: {e}")

                raw_mask_tile_path = os.path.join(
                    tile_dir, f'tile_r{row}_c{col}_raw_mask.ome.tiff'
                )
                try:
                    self._write_tile_ometiff(
                        raw_mask_tile_path,
                        raw_own_mask.astype(np.float32),
                        description=f'raw mask row={row} col={col} '
                                    f'n_cells={int(raw_own_mask.max())}',
                    )
                except Exception as e:
                    log.warning(f"  raw mask tile write failed: {e}")
                del raw_own_mask, dapi_own

                n_raw = int(local_mask.max())
                if n_raw == 0:
                    self.tile_done.emit(i, n_tiles, 0)
                    del local_mask
                    gc.collect()
                    self._drop_caches()
                    continue

                cy, cx = self._centroids_vectorised(local_mask)

                keep_labels = []
                for label_idx in range(n_raw):
                    lcy = cy[label_idx]
                    lcx = cx[label_idx]
                    if (lcy >= local_oy0 and lcy < local_oy1 and
                            lcx >= local_ox0 and lcx < local_ox1):
                        keep_labels.append(label_idx + 1)

                if not keep_labels:
                    self.tile_done.emit(i, n_tiles, 0)
                    del local_mask, cy, cx
                    gc.collect()
                    self._drop_caches()
                    continue

                lut = np.zeros(n_raw + 1, dtype=np.uint32)
                for new_id, lab in enumerate(keep_labels, start=1):
                    lut[lab] = new_id + global_id_offset

                remapped = lut[local_mask]
                del local_mask, lut, cy, cx

                dst = mmap[ry0:ry1, rx0:rx1]
                np.copyto(dst, remapped, where=(remapped > 0))
                del remapped

                n_kept = len(keep_labels)
                global_id_offset += n_kept
                tile_stats.append({
                    'row': row,
                    'col': col,
                    'n_cells': n_kept,
                    'bbox_local': [oy0, oy1, ox0, ox1],
                    'dapi_path': self._abs(dapi_tile_path),
                    'mask_path': self._abs(raw_mask_tile_path),
                })

                self.tile_done.emit(i, n_tiles, n_kept)
                _done_msg = (
                    f"✓ Tile [{i+1}/{n_tiles}]  kept={n_kept} cells  "
                    f"total so far={global_id_offset}"
                )
                self.progress.emit(i + 1, n_tiles, _done_msg)
                log.info(_done_msg)
                log.debug(f"  [MEM after write] {self._mem_snapshot()}")
                gc.collect()
                if self.recovery_npy_dir is None and torch.cuda.is_available():
                    torch.cuda.empty_cache()
                self._drop_caches()
                log.debug("  [MEM after drop_caches] " + self._mem_snapshot())

            if self.recovery_npy_dir is None:
                del model
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                self._drop_caches()
                log.info(f"All inference done. {self._mem_snapshot()}")

            mmap.flush()
            dapi_mmap.flush()
            total_cells = int(global_id_offset)
            _out_msg = f"Inference done. Total cells: {total_cells:,}. Writing outputs…"
            self.progress.emit(n_tiles, n_tiles, _out_msg)
            log.info(_out_msg)
            self._stop_mem_logger()

            del mmap, dapi_mmap
            gc.collect()
            self._drop_caches()

            mmap_ro      = np.memmap(mmap_path,      dtype='uint32', mode='r',
                                     shape=(full_h, full_w))
            dapi_mmap_ro = np.memmap(dapi_mmap_path, dtype='uint16', mode='r',
                                     shape=(full_h, full_w))

            CHUNK_ROWS = 4096
            n_chunks   = -(-full_h // CHUNK_ROWS)

            out_zarr_path = os.path.join(self.output_dir, 'global_mask.zarr')
            out_z = zarr.open(
                out_zarr_path, mode='w',
                shape=(full_h, full_w),
                dtype='uint32',
                chunks=(1024, 1024),
            )
            for ci, y in enumerate(range(0, full_h, CHUNK_ROWS)):
                y1 = min(y + CHUNK_ROWS, full_h)
                out_z[y:y1, :] = mmap_ro[y:y1, :]
                self.progress.emit(n_tiles, n_tiles,
                                   f'Writing mask zarr… chunk {ci+1}/{n_chunks}')
            self.progress.emit(n_tiles, n_tiles, '✓ mask zarr written')
            log.info(f"zarr → {out_zarr_path}  {self._mem_snapshot()}")
            self._drop_caches()

            ome_path = os.path.join(self.output_dir, 'global_mask.ome.tiff')
            self.progress.emit(n_tiles, n_tiles, 'Writing global mask OME-TIFF…')
            with tifffile.TiffWriter(ome_path, bigtiff=True) as tif:
                tif.write(
                    mmap_ro.astype(np.float32),
                    tile=(512, 512),
                    compression='lzw',
                    photometric='minisblack',
                    metadata=None,
                )
            self.progress.emit(n_tiles, n_tiles, '✓ global mask OME-TIFF written')
            log.info(f"mask OME-TIFF → {ome_path}  {self._mem_snapshot()}")
            self._drop_caches()

            global_dapi_path = os.path.join(self.output_dir, 'global_dapi.ome.tiff')
            self.progress.emit(n_tiles, n_tiles, 'Writing global DAPI OME-TIFF…')
            with tifffile.TiffWriter(global_dapi_path, bigtiff=True) as tif:
                tif.write(
                    np.array(dapi_mmap_ro),
                    tile=(512, 512),
                    compression='lzw',
                    photometric='minisblack',
                    metadata=None,
                )
            self.progress.emit(n_tiles, n_tiles, '✓ global DAPI OME-TIFF written')
            log.info(f"DAPI OME-TIFF → {global_dapi_path}  {self._mem_snapshot()}")

            del mmap_ro, dapi_mmap_ro
            gc.collect()
            self._drop_caches()
            log.debug(f"[MEM final drop_caches] {self._mem_snapshot()}")

            meta = {
                'mode':           'full_wsi',
                'result_id':      self.result_id,
                'method':         self.method,
                'display_name':   self.seg_config.get("display_name", self.method),
                'output_dir':     self._abs(self.output_dir),
                'project_output_dir': self._abs(self.project_output_dir),
                'zarr_path':      self._abs(out_zarr_path),
                'fused_zarr_path': self._abs(self.zarr_path),
                'input_zarr':      self._abs(self.zarr_path),
                'source_zarr':     self._abs(self.zarr_path),
                'ome_tiff':       self._abs(ome_path),
                'mask_path':      self._abs(ome_path),
                'global_dapi':    self._abs(global_dapi_path),
                'dapi_path':      self._abs(global_dapi_path),
                'tile_dir':       self._abs(tile_dir),
                'tiles_dir':       self._abs(tile_dir),
                'mmap_path':      self._abs(mmap_path),
                'total_cells':    total_cells,
                'image_shape':    [full_h, full_w],
                'tile_grid':      [self.n_rows, self.n_cols],
                'overlap_px':     self.overlap_px,
                'tile_stats':     tile_stats,
                'seg_config':     self.seg_config,
                'cp_params':      self.seg_config,
                'config_path':    self._abs(config_path),
                'created_at':     datetime.now().isoformat(),
            }
            with open(os.path.join(self.output_dir,
                                   'segmentation_meta.json'), 'w') as f:
                json.dump(meta, f, indent=2)
            self._register_completed_result(meta)

            log.info(
                f"=== Segmentation complete ===  "
                f"total_cells={total_cells:,}  "
                f"output={self.output_dir}"
            )
            log.info(f"Final memory: {self._mem_snapshot()}")
            self.finished.emit(self.output_dir, total_cells)

        except Exception:
            tb = traceback.format_exc()
            if self._logger:
                self._logger.critical("FATAL ERROR:\n" + tb)
                try:
                    self._logger.critical(
                        f"Memory at crash: {self._mem_snapshot()}"
                    )
                except Exception:
                    pass
            self._stop_mem_logger()
            self.error.emit(tb)
