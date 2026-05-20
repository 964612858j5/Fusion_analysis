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
import time
import subprocess
import csv
from datetime import datetime
from contextlib import nullcontext

import numpy as np
import tifffile
import zarr

from PyQt5.QtCore import QThread, pyqtSignal

from ..core.io_loader import OMETIFFLoader
from ..utils.segmentation_config import (
    CELLPOSE_NUCLEI_DAPI,
    CELLPOSE_NUCLEI_EXPANSION,
    CELLPOSE_NUCLEI_HQ,
    CELLPOSE_NUCLEI_HQ2,
    CELLPOSE_WHOLECELL_FUSION,
    MESMER_WHOLE_CELL,
    MESMER_NUCLEI,
    MESMER_NUCLEAR_GUIDED,
    STARDIST_NUCLEI_DAPI,
    STARDIST_NUCLEI_EXPANSION,
    normalize_segmentation_config,
)
from ..utils.segmentation_registry import (
    create_result_dir,
    register_legacy_result,
    upsert_result,
)
from ..utils.roi_project import (
    load_json,
    roi_manifest_path,
    update_roi_segmentation_run,
)
from ..utils.step2_profiler import Step2Profiler
from ..utils.channel_cache import SharedChannelStore
from ..utils.tile_prefetch import TilePrefetcher
from ..utils.tile_strategy import suggest_tile_strategy
from .cellpose_worker import load_stardist_model
from .mesmer_worker import run_mesmer_on_channel_source, run_mesmer_on_fused_tile
from ..utils.mesmer_utils import get_mesmer_device_status, load_mesmer_application, mesmer_metadata
from .hq_marker_segmentation import (
    parse_hq_channels,
    resolve_hq_channels,
    segment_nuclei_hq,
    validate_hq_channels,
    write_hq_qc_table,
)
from .hq2_marker_segmentation import (
    hq2_metadata_fields,
    run_hq2_segmentation,
    write_hq2_qc_table,
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
                 rois=None, cp_params=None, param_file=None,
                 parameter_source="manual"):
        super().__init__()
        self.zarr_path        = zarr_path
        self.seg_config       = normalize_segmentation_config(seg_config if seg_config is not None else cp_params)
        self.method           = self.seg_config.get("method", CELLPOSE_WHOLECELL_FUSION)
        self.n_rows           = n_rows
        self.n_cols           = n_cols
        self.overlap_px       = overlap_px
        self.project_output_dir = os.path.abspath(output_dir or os.getcwd())
        self.roi_dir = self._infer_roi_dir(self.project_output_dir)
        self.roi_manifest = load_json(roi_manifest_path(self.roi_dir), {}) if self.roi_dir else {}
        self.roi_id = str(self.roi_manifest.get("roi_id") or self._roi_id_from_rois(rois) or "")
        self.roi_display_name = str(self.roi_manifest.get("display_name") or self._roi_display_from_rois(rois) or "ROI_1")
        self.result_id, self.output_dir, self.created_at = self._create_output_dir()
        self.recovery_npy_dir = recovery_npy_dir
        self.rois             = rois
        self.param_file       = self._abs(param_file) if param_file else ""
        self.parameter_source = "index" if self.param_file and parameter_source == "index" else "manual"
        self._stop            = False
        self._logger          = None
        self._mem_timer       = None
        self._mem_log_active  = False
        self._run_started_at  = None
        self._run_finished_at = None
        self._peak_ram_bytes  = 0
        self._peak_vram_bytes = 0
        self._runtime_summary = {}
        self._resource_samples_path = ""
        self._runtime_partial_path = ""
        self._resource_sample_header_written = False
        self._last_region_meta = None
        self._current_region_bbox = None
        self._hq_resolved_source_path = ""
        self.write_tile_tiffs = self._config_bool("write_tile_tiffs", False)
        self.write_hq2_debug_layers = self._config_bool("write_hq2_debug_layers", False)
        self.write_hq2_debug_tiffs = self._config_bool("write_hq2_debug_tiffs", False)
        self._hq2_tile_metadata = []
        self.step2_profiler = Step2Profiler(
            enabled=self._step2_profiling_enabled(),
            output_dir=self.output_dir,
            run_id=self.result_id,
            method=self.method,
        )
        self._channel_store = SharedChannelStore(
            max_cache_items=int(self.seg_config.get("channel_cache_items", 32) or 32),
            logger=self._logger,
        )
        self._tile_strategy_info = {}

    def _step2_profiling_enabled(self):
        env = str(os.environ.get("FUSION_STEP2_PROFILE", "")).strip().lower()
        if env in {"0", "false", "no", "off"}:
            return False
        if env in {"1", "true", "yes", "on"}:
            return True
        return self._config_bool("enable_step2_profiling", True)

    def _config_bool(self, key, default=False):
        value = self.seg_config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _infer_roi_dir(output_dir):
        cur = os.path.abspath(output_dir or "")
        while cur and cur != os.path.dirname(cur):
            if os.path.exists(os.path.join(cur, "roi_manifest.json")):
                return cur
            cur = os.path.dirname(cur)
        return ""

    @staticmethod
    def _roi_id_from_rois(rois):
        for roi in rois or []:
            if roi.get("roi_id"):
                return roi.get("roi_id")
        return ""

    @staticmethod
    def _roi_display_from_rois(rois):
        for roi in rois or []:
            if roi.get("display_name") or roi.get("name"):
                return roi.get("display_name") or roi.get("name")
        return ""

    def _create_output_dir(self):
        now = datetime.now()
        created_at = now.isoformat()
        safe_method = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(self.method)).strip("._")
        if self.roi_dir or os.path.basename(self.project_output_dir) == "step2":
            base = f"seg_{now.strftime('%Y%m%d_%H%M%S')}_{safe_method}"
            parent = os.path.join(self.project_output_dir, "segmentation_runs")
            os.makedirs(parent, exist_ok=True)
            run_id = base
            out_dir = os.path.join(parent, run_id)
            suffix = 1
            while os.path.exists(out_dir):
                run_id = f"{base}_{suffix:02d}"
                out_dir = os.path.join(parent, run_id)
                suffix += 1
            os.makedirs(out_dir, exist_ok=True)
            return run_id, out_dir, created_at
        return create_result_dir(self.project_output_dir, self.method)

    @staticmethod
    def _abs(path):
        return os.path.abspath(path) if path else path

    def _project_path(self, *parts):
        return os.path.join(self.project_output_dir, *parts)

    def _load_param_file_config(self):
        if not self.param_file or not os.path.exists(self.param_file):
            return {}
        try:
            with open(self.param_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_run_segmentation_config(self):
        path = os.path.join(self.output_dir, "run_segmentation_params.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.seg_config, f, indent=2)
        return path

    def _write_run_metadata(self, summary_meta):
        meta = {
            "run_id": self.result_id,
            "method": self.seg_config.get("method", self.method),
            "parameter_source": self.parameter_source,
            "param_file": self.param_file or None,
            "created_at": self.created_at,
            "input_zarr": self._abs(self.zarr_path),
            "output_dir": self._abs(self.output_dir),
            "roi_mode": bool(self.rois),
            "n_rois": len(self.rois or []),
        }
        meta.update({k: v for k, v in (summary_meta or {}).items() if k not in meta})
        path = os.path.join(self.output_dir, "run_metadata.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return path

    def _update_results_index(self, entry):
        path = os.path.join(self.project_output_dir, "segmentation_results", "segmentation_results_index.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"version": 1, "runs": [], "latest_by_method": {}}
        runs = data.setdefault("runs", [])
        rid = entry.get("result_id")
        runs[:] = [r for r in runs if r.get("run_id") != rid and r.get("result_id") != rid]
        runs.append({
            "run_id": rid,
            "result_id": rid,
            "method": entry.get("method"),
            "display_name": entry.get("display_name"),
            "created_at": entry.get("created_at"),
            "status": entry.get("status"),
            "output_dir": entry.get("output_dir"),
            "config_path": entry.get("config_path"),
            "meta_path": entry.get("meta_path"),
            "param_file": self.param_file,
        })
        data.setdefault("latest_by_method", {})[entry.get("method")] = rid
        data["updated_at"] = datetime.now().isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return path

    def _multichannel_source_path(self):
        param_cfg = self._load_param_file_config()
        explicit = [
            self.seg_config.get("hq_source_zarr"),
            self.seg_config.get("multichannel_source_path"),
            param_cfg.get("hq_source_zarr"),
            param_cfg.get("multichannel_source_path"),
            param_cfg.get("corrected_channels_zarr"),
        ]
        for path in explicit:
            if path:
                return self._abs(path)
        candidates = [
            os.path.join(self.roi_dir, "step0", "corrected_channels.zarr") if self.roi_dir else "",
            self.roi_manifest.get("corrected_zarr_path") or "",
            self._project_path("corrected_channels.zarr"),
            os.path.join(os.path.dirname(self.project_output_dir), "corrected_channels.zarr"),
        ]
        seen = set()
        for path in candidates:
            if path and os.path.exists(path):
                if path in seen:
                    continue
                seen.add(path)
                return self._abs(path)
        return ""

    def _raw_channel_source_path(self):
        param_cfg = self._load_param_file_config()
        for path in (
            self.seg_config.get("raw_channel_source_path"),
            self.seg_config.get("raw_ome_path"),
            param_cfg.get("raw_channel_source_path"),
            param_cfg.get("raw_ome_path"),
            self.roi_manifest.get("source_ome"),
            self.roi_manifest.get("raw_ome_path"),
        ):
            if path and os.path.exists(path):
                return self._abs(path)
        return ""

    def _fusion_source_path(self, roi_name=None):
        candidates = [
            os.path.join(self.roi_dir, "step1", "fused.zarr") if self.roi_dir else "",
            os.path.join(self.roi_dir, "step1", f"fused_{roi_name}.zarr") if self.roi_dir and roi_name else "",
            self.zarr_path,
            self._project_path(f"fused_{roi_name}.zarr") if roi_name else "",
            self._project_path("fused.zarr"),
            os.path.join(os.path.dirname(self.project_output_dir), f"fused_{roi_name}.zarr") if roi_name else "",
            os.path.join(os.path.dirname(self.project_output_dir), "fused.zarr"),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return self._abs(path)
        return self._abs(self.zarr_path)

    def _register_completed_result(self, summary_meta):
        config_path = os.path.join(self.output_dir, "run_segmentation_params.json")
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
        run_meta_path = self._write_run_metadata(summary_meta)
        entry["run_metadata_path"] = self._abs(run_meta_path)
        entry["param_file"] = self.param_file
        upsert_result(self.project_output_dir, entry)
        self._update_results_index(entry)
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
        if getattr(self, "_channel_store", None) is not None:
            self._channel_store.logger = logger
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

    @staticmethod
    def _format_bytes(n_bytes):
        if n_bytes is None:
            return "N/A"
        try:
            value = float(n_bytes)
        except Exception:
            return "N/A"
        if value <= 0:
            return "N/A"
        units = ("B", "KB", "MB", "GB", "TB")
        idx = 0
        while value >= 1024 and idx < len(units) - 1:
            value /= 1024.0
            idx += 1
        return f"{value:.2f} {units[idx]}"

    @staticmethod
    def _format_duration(seconds):
        try:
            seconds = float(seconds)
        except Exception:
            return "N/A"
        if seconds < 0:
            return "N/A"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds - h * 3600 - m * 60
        if h:
            return f"{h}h {m:02d}m {s:04.1f}s"
        if m:
            return f"{m}m {s:04.1f}s"
        return f"{s:.1f}s"

    def _uses_torch_backend(self):
        return self.method in (
            CELLPOSE_WHOLECELL_FUSION,
            CELLPOSE_NUCLEI_DAPI,
            CELLPOSE_NUCLEI_EXPANSION,
            CELLPOSE_NUCLEI_HQ,
            CELLPOSE_NUCLEI_HQ2,
        )

    @staticmethod
    def _empty_torch_cache_if_available():
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _current_process_ram_bytes(self):
        """Return RSS for this process plus live child processes."""
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            rss = proc.memory_info().rss
            for child in proc.children(recursive=True):
                try:
                    rss += child.memory_info().rss
                except Exception:
                    pass
            return int(rss)
        except Exception:
            return 0

    def _current_vram_bytes(self):
        """Return best-effort VRAM currently used by this run's process tree."""
        max_bytes = 0
        try:
            import torch
            if torch.cuda.is_available():
                current_total = 0
                peak_total = 0
                for i in range(torch.cuda.device_count()):
                    current_total += int(torch.cuda.memory_reserved(i))
                    peak_total += int(torch.cuda.max_memory_reserved(i))
                max_bytes = max(max_bytes, current_total, peak_total)
        except Exception:
            pass

        try:
            import psutil
            pids = {os.getpid()}
            pids.update(child.pid for child in psutil.Process(os.getpid()).children(recursive=True))
            cmd = [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ]
            out = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
                check=False,
            )
            total_mb = 0
            for line in (out.stdout or "").splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                try:
                    pid = int(parts[0])
                    used_mb = float(parts[1])
                except Exception:
                    continue
                if pid in pids:
                    total_mb += used_mb
            if total_mb > 0:
                max_bytes = max(max_bytes, int(total_mb * 1024 * 1024))
        except Exception:
            pass
        return int(max_bytes)

    def _sample_runtime_resources(self):
        ram = self._current_process_ram_bytes()
        vram = self._current_vram_bytes()
        if ram > self._peak_ram_bytes:
            self._peak_ram_bytes = ram
        if vram > self._peak_vram_bytes:
            self._peak_vram_bytes = vram
        now = time.time()
        elapsed = 0.0
        if self._run_started_at is not None:
            elapsed = max(0.0, now - self._run_started_at)
        sample = {
            "timestamp": datetime.fromtimestamp(now).isoformat(),
            "elapsed_seconds": elapsed,
            "ram_bytes": int(ram),
            "ram": self._format_bytes(ram),
            "peak_ram_bytes": int(self._peak_ram_bytes),
            "peak_ram": self._format_bytes(self._peak_ram_bytes),
            "vram_bytes": int(vram),
            "vram": self._format_bytes(vram),
            "peak_vram_bytes": int(self._peak_vram_bytes),
            "peak_vram": self._format_bytes(self._peak_vram_bytes),
        }
        self._write_runtime_sample(sample)
        return sample

    def _write_runtime_sample(self, sample):
        if not self._resource_samples_path:
            return
        try:
            os.makedirs(os.path.dirname(self._resource_samples_path), exist_ok=True)
            fieldnames = [
                "timestamp",
                "elapsed_seconds",
                "ram_bytes",
                "ram",
                "peak_ram_bytes",
                "peak_ram",
                "vram_bytes",
                "vram",
                "peak_vram_bytes",
                "peak_vram",
            ]
            write_header = (
                not self._resource_sample_header_written
                or not os.path.exists(self._resource_samples_path)
                or os.path.getsize(self._resource_samples_path) == 0
            )
            with open(self._resource_samples_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if write_header:
                    writer.writeheader()
                    self._resource_sample_header_written = True
                writer.writerow({key: sample.get(key) for key in fieldnames})
                f.flush()
                os.fsync(f.fileno())
        except Exception as exc:
            if self._logger:
                self._logger.debug("Failed to write resource sample: %s", exc)

        if self._runtime_partial_path:
            try:
                os.makedirs(os.path.dirname(self._runtime_partial_path), exist_ok=True)
                partial = {
                    "status": "running",
                    "method": self.method,
                    "run_id": self.result_id,
                    "output_dir": self._abs(self.output_dir),
                    "resource_samples_csv": self._abs(self._resource_samples_path),
                    "last_sample": sample,
                    "elapsed_seconds": sample.get("elapsed_seconds", 0.0),
                    "elapsed": self._format_duration(sample.get("elapsed_seconds", 0.0)),
                    "peak_ram_bytes": int(self._peak_ram_bytes),
                    "peak_ram": self._format_bytes(self._peak_ram_bytes),
                    "peak_vram_bytes": int(self._peak_vram_bytes),
                    "peak_vram": self._format_bytes(self._peak_vram_bytes),
                    "updated_at": datetime.now().isoformat(),
                }
                tmp_path = self._runtime_partial_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(partial, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._runtime_partial_path)
            except Exception as exc:
                if self._logger:
                    self._logger.debug("Failed to write runtime partial: %s", exc)

    def _start_runtime_monitor(self, interval_s=2):
        """Start a daemon sampler for elapsed time plus peak RAM/VRAM."""
        self._run_started_at = time.time()
        self._run_finished_at = None
        self._peak_ram_bytes = 0
        self._peak_vram_bytes = 0
        self._runtime_summary = {}
        self._resource_samples_path = os.path.join(self.output_dir, "resource_samples.csv")
        self._runtime_partial_path = os.path.join(self.output_dir, "runtime_partial.json")
        self._resource_sample_header_written = False
        try:
            import torch
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    torch.cuda.reset_peak_memory_stats(i)
        except Exception:
            pass
        self._mem_log_active = True
        self._sample_runtime_resources()
        if self._logger:
            self._logger.info("resource_samples.csv -> %s", self._resource_samples_path)
            self._logger.info("runtime_partial.json -> %s", self._runtime_partial_path)

        def _loop():
            while self._mem_log_active and not self._stop:
                self._sample_runtime_resources()
                if self._logger:
                    self._logger.debug(f"[MEM] {self._mem_snapshot()}")
                time.sleep(interval_s)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        self._mem_timer = t

    def _finish_runtime_monitor(self):
        self._sample_runtime_resources()
        self._mem_log_active = False
        self._run_finished_at = time.time()
        elapsed = 0.0
        if self._run_started_at is not None:
            elapsed = max(0.0, self._run_finished_at - self._run_started_at)
        self._runtime_summary = {
            "elapsed_seconds": elapsed,
            "elapsed": self._format_duration(elapsed),
            "peak_ram_bytes": int(self._peak_ram_bytes),
            "peak_ram": self._format_bytes(self._peak_ram_bytes),
            "peak_vram_bytes": int(self._peak_vram_bytes),
            "peak_vram": self._format_bytes(self._peak_vram_bytes),
            "resource_samples_csv": self._abs(self._resource_samples_path) if self._resource_samples_path else "",
            "runtime_partial_json": self._abs(self._runtime_partial_path) if self._runtime_partial_path else "",
        }
        if self._runtime_partial_path:
            try:
                os.makedirs(os.path.dirname(self._runtime_partial_path), exist_ok=True)
                final_partial = dict(self._runtime_summary)
                final_partial.update({
                    "status": "finished",
                    "method": self.method,
                    "run_id": self.result_id,
                    "output_dir": self._abs(self.output_dir),
                    "updated_at": datetime.now().isoformat(),
                })
                tmp_path = self._runtime_partial_path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(final_partial, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._runtime_partial_path)
            except Exception as exc:
                if self._logger:
                    self._logger.debug("Failed to finalize runtime partial: %s", exc)
        if self._logger:
            self._logger.info(
                "[Step2 runtime] elapsed=%s  peak_RAM=%s  peak_VRAM=%s  samples=%s",
                self._runtime_summary["elapsed"],
                self._runtime_summary["peak_ram"],
                self._runtime_summary["peak_vram"],
                self._runtime_summary["resource_samples_csv"],
            )
        print(
            "[Step2 runtime] "
            f"elapsed={self._runtime_summary['elapsed']}  "
            f"peak_RAM={self._runtime_summary['peak_ram']}  "
            f"peak_VRAM={self._runtime_summary['peak_vram']}  "
            f"samples={self._runtime_summary['resource_samples_csv']}"
        )
        return dict(self._runtime_summary)

    def runtime_summary(self):
        return dict(self._runtime_summary or {})

    def _profile_summary(self, summary):
        if not summary:
            return
        try:
            msg = (
                "[Step2Profile] "
                f"total={summary.get('total_runtime', 0.0):.1f}s "
                f"bottleneck={summary.get('suspected_bottleneck') or 'unknown'} "
                f"slowest_tile={summary.get('slowest_tile')}"
            )
            print(msg)
            if self._logger:
                self._logger.info(msg)
                if summary.get("summary_path"):
                    self._logger.info("[Step2Profile] summary -> %s", summary.get("summary_path"))
        except Exception:
            pass

    def _profile_tile_line(self, tile_id, n_tiles, stage_seconds, labels_count, tile_shape):
        try:
            if not getattr(self.step2_profiler, "enabled", False):
                return
            profile_tile_id = stage_seconds.pop("_profile_tile_id", None)
            if profile_tile_id is not None:
                profiled = self.step2_profiler._tile_stage_totals.get(str(profile_tile_id), {}) or {}
                if profiled:
                    stage_seconds = {key: float(value or 0.0) for key, value in profiled.items()}
            total = sum(float(v or 0.0) for v in stage_seconds.values())
            mem = self.step2_profiler.snapshot_memory() or {}
            msg = (
                f"[Step2Profile] tile={int(tile_id) + 1}/{n_tiles} "
                f"read={stage_seconds.get('read_tile', 0.0):.1f}s "
                f"prep={stage_seconds.get('preprocess', 0.0):.1f}s "
                f"infer={stage_seconds.get('model_inference', 0.0):.1f}s "
                f"post={stage_seconds.get('postprocess', 0.0):.1f}s "
                f"merge={stage_seconds.get('merge_or_write', 0.0):.1f}s "
                f"total={total:.1f}s labels={int(labels_count or 0)} "
                f"shape={tuple(tile_shape or ())} rss={mem.get('rss_mb', 0.0):.0f}MB"
            )
            print(msg)
            if self._logger:
                self._logger.info(msg)
        except Exception:
            pass

    def _read_dapi_from_zarr(self, z, y0, y1, x0, x1):
        """
        Read the nucleus (DAPI) channel directly from fused zarr channel index 1.
        fused zarr shape: (H, W, 2)  ch0=cyto  ch1=nucleus(DAPI)  dtype=uint16
        Returns uint16 ndarray (H, W).
        """
        source = getattr(z, "store", None)
        source_path = getattr(source, "path", None) or getattr(source, "dir_path", None)
        if source_path and getattr(self, "_channel_store", None) is not None:
            return self._channel_store.read_dapi(source_path, y0, y1, x0, x1)
        return np.array(z[y0:y1, x0:x1, 1])

    def _build_step2_tiles(self, full_h, full_w):
        tile_h = -(-int(full_h) // int(self.n_rows))
        tile_w = -(-int(full_w) // int(self.n_cols))
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
        return tile_h, tile_w, tiles

    def _record_tile_strategy(self, full_h, full_w, channel_count=1):
        suggested = suggest_tile_strategy(
            full_h,
            full_w,
            self.method,
            vram_gb=self._available_vram_gb(),
            channel_count=channel_count,
            target_tile_mpx=self.seg_config.get("target_tile_mpx"),
        )
        actual_tile_h = -(-int(full_h) // int(self.n_rows))
        actual_tile_w = -(-int(full_w) // int(self.n_cols))
        mode = str(self.seg_config.get("tile_strategy_mode") or "manual")
        info = {
            "tile_strategy_mode": mode,
            "suggested_tile_h": int(suggested["tile_h"]),
            "suggested_tile_w": int(suggested["tile_w"]),
            "suggested_n_rows": int(suggested["n_rows"]),
            "suggested_n_cols": int(suggested["n_cols"]),
            "suggested_overlap": int(suggested["overlap"]),
            "actual_tile_h": int(actual_tile_h),
            "actual_tile_w": int(actual_tile_w),
            "estimated_tile_mpx": float(actual_tile_h * actual_tile_w / 1e6),
            "estimated_vram_usage": float(suggested.get("estimated_vram_usage") or 0.0),
        }
        self._tile_strategy_info = info
        msg = (
            "[TileStrategy] "
            f"backend={self.method} suggested={suggested['n_rows']}x{suggested['n_cols']} "
            f"(~{suggested['estimated_tile_mpx']:.0f}MP/tile) "
            f"actual={self.n_rows}x{self.n_cols}"
        )
        print(msg)
        if self._logger:
            self._logger.info(msg)
        return info

    def _step2_engine_meta(self):
        cache = {}
        try:
            cache = self._channel_store.snapshot_metrics() if self._channel_store is not None else {}
        except Exception:
            cache = {}
        return {
            **dict(self._tile_strategy_info or {}),
            "channel_cache": cache,
        }

    def _record_engine_metrics(self):
        try:
            metrics = self._channel_store.snapshot_metrics() if self._channel_store is not None else {}
            if metrics:
                self.step2_profiler.log_tile_stage(None, "cache_lookup", 0.0, **metrics)
                msg = (
                    "[Step2Engine] "
                    f"cache_hit={metrics.get('cache_hit', 0)} "
                    f"cache_miss={metrics.get('cache_miss', 0)} "
                    f"cache_bytes={metrics.get('cache_bytes', 0)}"
                )
                print(msg)
                if self._logger:
                    self._logger.info(msg)
        except Exception:
            pass

    @staticmethod
    def _prof_metrics(base):
        metrics = dict(base or {})
        metrics.pop("tile_id", None)
        metrics.pop("stage", None)
        metrics.pop("seconds", None)
        return metrics

    @staticmethod
    def _available_vram_gb():
        try:
            import torch
            if torch.cuda.is_available():
                return torch.cuda.get_device_properties(0).total_memory / (1024.0 ** 3)
        except Exception:
            pass
        return None

    def _prefetch_enabled(self):
        if self.recovery_npy_dir is not None:
            return False
        return self._config_bool("enable_tile_prefetch", True)

    def _prepare_tile_payload(self, zarr_path, z, tile, is_hq, is_mesmer_guided,
                              hq_group, hq_channels, is_mesmer, mesmer_group,
                              dapi_mmap=None, profile_tile_base=None):
        ry0, ry1, rx0, rx1 = tile['read']
        oy0, oy1, ox0, ox1 = tile['own']
        own_h = oy1 - oy0
        own_w = ox1 - ox0
        profile_tile_base = profile_tile_base or {}
        cache_ctx = self.step2_profiler.time_stage("cache_lookup", **profile_tile_base) if profile_tile_base else nullcontext()
        with cache_ctx:
            if getattr(self, "_channel_store", None) is not None:
                tile_data = self._channel_store.read_fused(zarr_path, ry0, ry1, rx0, rx1)
                dapi_own = self._channel_store.read_dapi(zarr_path, oy0, oy1, ox0, ox1)
            else:
                tile_data = np.array(z[ry0:ry1, rx0:rx1, :])
                dapi_own = self._read_dapi_from_zarr(z, oy0, oy1, ox0, ox1)
        if dapi_mmap is not None:
            dapi_mmap[oy0:oy1, ox0:ox1] = dapi_own[:own_h, :own_w]
        hq_marker_channels = None
        mesmer_channel_source = None
        prepare_ctx = self.step2_profiler.time_stage("tile_prepare", **profile_tile_base) if profile_tile_base else nullcontext()
        with prepare_ctx:
            if is_hq and not is_mesmer_guided:
                hq_marker_channels = self._read_hq_marker_channels(hq_group, hq_channels, ry0, ry1, rx0, rx1)
            if is_mesmer and mesmer_group is not None:
                mesmer_channel_source = self._read_mesmer_channel_source(mesmer_group, ry0, ry1, rx0, rx1)
        return {
            "tile_data": tile_data,
            "dapi_own": dapi_own,
            "hq_marker_channels": hq_marker_channels,
            "mesmer_channel_source": mesmer_channel_source,
        }

    @staticmethod
    def _group_keys(root):
        groups = list(getattr(root, "group_keys", lambda: [])())
        if not groups and hasattr(root, "groups"):
            groups = [name for name, _group in root.groups()]
        return groups

    def _available_groups_debug(self, root):
        debug = []
        for group_name in self._group_keys(root):
            group = root[group_name]
            debug.append(
                {
                    "group": group_name,
                    "attrs": dict(group.attrs),
                    "channels": self._channel_array_names(group),
                }
            )
        return debug

    def _requested_roi_names(self, roi_name=None):
        names = [
            roi_name,
            self.seg_config.get("roi_name"),
            self.seg_config.get("roi_display_name"),
            self.roi_display_name,
        ]
        return {str(name) for name in names if str(name or "").strip()}

    def _open_hq_channel_group(self, roi_name=None):
        path = self._multichannel_source_path()
        if not path or not os.path.exists(path):
            raw_path = self._raw_channel_source_path()
            if raw_path:
                self._hq_resolved_source_path = raw_path
                return {"kind": "raw_ome", "path": raw_path, "loader": OMETIFFLoader(raw_path)}
            raise FileNotFoundError(
                "Cellpose nuclei + HQ requires corrected_channels.zarr or raw OME channel source, but neither was found.\n"
                f"Requested HQ source: {path or '(none)'}\n"
                f"Resolved raw source: {raw_path or '(none)'}\n"
                f"loaded param_file path: {self.param_file or '(none)'}"
            )
        self._hq_resolved_source_path = self._abs(path)
        root = zarr.open(path, mode="r")
        mode = str(root.attrs.get("mode", "")).strip().lower()
        if mode == "roi_only":
            groups = self._group_keys(root)
            requested_roi_id = str(self.seg_config.get("roi_id") or self.roi_id or "")
            requested_names = self._requested_roi_names(roi_name)
            for group_name in groups:
                group = root[group_name]
                if requested_roi_id and str(group.attrs.get("roi_id") or "") == requested_roi_id:
                    return group
            for group_name in groups:
                group = root[group_name]
                group_names = {
                    str(group_name),
                    str(group.attrs.get("roi_name") or ""),
                    str(group.attrs.get("display_name") or ""),
                    str(group.attrs.get("roi_display_name") or ""),
                }
                if requested_names and requested_names & {name for name in group_names if name}:
                    return group
            raise ValueError(
                "Could not match ROI group in corrected_channels.zarr for HQ segmentation.\n"
                f"Found corrected_channels.zarr at: {path}\n"
                f"Requested ROI id: {requested_roi_id or '(none)'}\n"
                f"Requested ROI name(s): {sorted(requested_names) if requested_names else '(none)'}\n"
                f"Available groups: {groups}\n"
                f"Available channels per group: {json.dumps(self._available_groups_debug(root), indent=2, default=str)}"
            )
        return root

    @staticmethod
    def _channel_array_names(group):
        if isinstance(group, dict) and group.get("kind") == "raw_ome":
            return group["loader"].channel_names()
        if hasattr(group, "array_keys"):
            return list(group.array_keys())
        return [k for k in group.keys() if hasattr(group[k], "shape")]

    def _validate_hq_config(self, roi_name=None):
        mode = str(self.seg_config.get("hq_input_mode") or "selected_channels_from_source").strip()
        if mode not in {"selected_channels_from_source", "step1_weighted_fusion", "hybrid"}:
            mode = "selected_channels_from_source"
        requested = parse_hq_channels(self.seg_config.get("hq_channels") or [])
        fusion_weights = dict(self.seg_config.get("step1_fusion_weights") or self.seg_config.get("channel_weights") or {})
        channels = requested if mode != "step1_weighted_fusion" else [ch for ch, w in fusion_weights.items() if float(w or 0) > 0]
        group = self._open_hq_channel_group(roi_name)
        available = self._channel_array_names(group)
        source_path = self._hq_resolved_source_path or self._multichannel_source_path() or self._raw_channel_source_path()
        resolved, missing, warnings = resolve_hq_channels(channels, available)
        if missing and not (isinstance(group, dict) and group.get("kind") == "raw_ome"):
            raw_path = self._raw_channel_source_path()
            if raw_path:
                try:
                    raw_loader = OMETIFFLoader(raw_path)
                    raw_available = raw_loader.channel_names()
                    raw_resolved, raw_missing, raw_warnings = resolve_hq_channels(channels, raw_available)
                    if not raw_missing:
                        print(
                            "[Step2-HQ] corrected zarr missing requested channels; "
                            f"falling back to raw OME channel source: {raw_path}"
                        )
                        group = {"kind": "raw_ome", "path": raw_path, "loader": raw_loader}
                        available = raw_available
                        source_path = self._abs(raw_path)
                        self._hq_resolved_source_path = source_path
                        resolved, missing, warnings = raw_resolved, raw_missing, raw_warnings
                except Exception as exc:
                    print(f"[Step2-HQ] failed to inspect raw OME fallback source {raw_path}: {exc}")
        root_attrs = {}
        if source_path and os.path.exists(source_path) and str(source_path).endswith(".zarr"):
            try:
                root_attrs = dict(zarr.open(source_path, mode="r").attrs)
            except Exception:
                root_attrs = {}
        group_name = "raw_ome" if isinstance(group, dict) else (getattr(group, "name", "") or "")
        group_attrs = {} if isinstance(group, dict) else dict(getattr(group, 'attrs', {}))
        context = (
            "HQ source debug:\n"
            f"  loaded param_file path: {self.param_file or '(none)'}\n"
            f"  selected hq_source_zarr: {source_path or '(none)'}\n"
            f"  requested channels: {channels}\n"
            f"  zarr attrs mode: {root_attrs}\n"
            f"  requested roi_id: {self.seg_config.get('roi_id') or self.roi_id or '(none)'}\n"
            f"  requested roi_name: {self.seg_config.get('roi_name') or self.seg_config.get('roi_display_name') or roi_name or self.roi_display_name or '(none)'}\n"
            f"  selected ROI group name: {group_name or '(root)'}\n"
            f"  selected ROI group attrs: {group_attrs}\n"
            f"  available channels from full source: {available}\n"
            f"  available channels from Step1 fusion weights: {sorted(fusion_weights.keys())}"
        )
        print(f"[Step2-HQ] hq_input_mode: {mode}")
        print(f"[Step2-HQ] requested hq_channels: {requested}")
        print(f"[Step2-HQ] full source path: {source_path or '(none)'}")
        print(f"[Step2-HQ] available channels from full source: {available}")
        print(f"[Step2-HQ] available channels from fusion weights: {sorted(fusion_weights.keys())}")
        print(f"[Step2-HQ] resolved channels: {resolved}")
        print(f"[Step2-HQ] missing channels: {missing}")
        if warnings:
            for msg in warnings:
                print(f"[Step2-HQ] warning: {msg}")
        channels = validate_hq_channels(channels, available, context=context)
        if mode == "hybrid":
            weights = {ch: float(fusion_weights.get(ch, 1.0)) for ch in channels}
            self.seg_config["channel_weights"] = weights
        elif mode == "step1_weighted_fusion":
            weights = {ch: float(fusion_weights.get(ch, 1.0)) for ch in channels}
            self.seg_config["channel_weights"] = weights
        self.seg_config["hq_channels"] = channels
        self.seg_config["hq_input_mode"] = mode
        if self._logger:
            root = zarr.open(source_path, mode="r") if source_path and str(source_path).endswith(".zarr") else None
            self._logger.debug("[HQ] loaded param_file path: %s", self.param_file or "")
            self._logger.debug("[HQ] seg_config hq_channels: %s", channels)
            self._logger.debug("[HQ] selected hq_source_zarr: %s", source_path)
            self._logger.debug("[HQ] zarr attrs mode: %s", dict(root.attrs) if root is not None else {})
            self._logger.debug("[HQ] selected ROI group name: %s", group_name or "(root)")
            self._logger.debug("[HQ] selected ROI group attrs: %s", group_attrs)
            self._logger.debug("[HQ] available channel array_keys: %s", available)
        return channels, group

    def _mesmer_uses_selected_channels(self):
        mode = str(self.seg_config.get("input_mode") or "selected_channels").strip().lower()
        return mode in {
            "selected_channels",
            "dapi + membrane",
            "dapi + selected channels",
            "membrane",
        }

    def _validate_mesmer_config(self, roi_name=None):
        if not self._mesmer_uses_selected_channels():
            self.seg_config["mesmer_input_source"] = "fused_zarr"
            return None

        nuclear = str(self.seg_config.get("nuclear_channel") or "DAPI").strip() or "DAPI"
        membrane_channels = parse_hq_channels(self.seg_config.get("membrane_channels") or [])
        requested = [nuclear] + [ch for ch in membrane_channels if ch != nuclear]
        group = self._open_hq_channel_group(roi_name)
        available = self._channel_array_names(group)
        source_path = self._hq_resolved_source_path or self._multichannel_source_path() or self._raw_channel_source_path()
        context = (
            "Mesmer source debug:\n"
            f"  loaded param_file path: {self.param_file or '(none)'}\n"
            f"  selected channel source: {source_path or '(none)'}\n"
            f"  input_mode: {self.seg_config.get('input_mode')}\n"
            f"  nuclear_channel: {nuclear}\n"
            f"  membrane_channels: {membrane_channels}\n"
            f"  requested roi_id: {self.seg_config.get('roi_id') or self.roi_id or '(none)'}\n"
            f"  requested roi_name: {self.seg_config.get('roi_name') or self.seg_config.get('roi_display_name') or roi_name or self.roi_display_name or '(none)'}\n"
            f"  available channels: {available}"
        )
        validate_hq_channels(requested, available, context=context)
        self.seg_config["nuclear_channel"] = nuclear
        self.seg_config["membrane_channels"] = membrane_channels
        self.seg_config["mesmer_input_source"] = "selected_channels_from_source"
        if self._logger:
            self._logger.info("[Mesmer] input source=selected_channels_from_source")
            self._logger.info("[Mesmer] nuclear channel=%s", nuclear)
            self._logger.info("[Mesmer] membrane channels=%s", membrane_channels)
            self._logger.info("[Mesmer] selected channel source=%s", source_path)
        return group

    def _hq_meta_fields(self, nuclei_mask_path="", final_cell_mask_path="", qc_table_path=""):
        return {
            "method": "cellpose_nuclei_hq",
            "display_name": "Cellpose nuclei + HQ",
            "cellpose_nuclei_parameters": {
                "model_type": self.seg_config.get("model_type", "cpsam"),
                "diameter": self.seg_config.get("diameter"),
                "flow_threshold": self.seg_config.get("flow_threshold", 0.4),
                "cellprob_threshold": self.seg_config.get("cellprob_threshold", 0.0),
                "min_size": self.seg_config.get("min_size", 15),
            },
            "hq_input_mode": self.seg_config.get("hq_input_mode", "selected_channels_from_source"),
            "hq_channels": parse_hq_channels(self.seg_config.get("hq_channels") or []),
            "max_cell_radius": self.seg_config.get("max_cell_radius", 12),
            "normalization_percentiles": [
                self.seg_config.get("normalization_percentile_low", 1.0),
                self.seg_config.get("normalization_percentile_high", 99.5),
            ],
            "consensus_mode": self.seg_config.get("consensus_mode", "adaptive_best_channel"),
            "channel_weights": self.seg_config.get("channel_weights") or {},
            "min_signal_threshold": self.seg_config.get("min_signal_threshold", 0.08),
            "nuclei_mask_path": self._abs(nuclei_mask_path),
            "final_cell_mask_path": self._abs(final_cell_mask_path),
            "qc_table_path": self._abs(qc_table_path),
        }

    def _write_label_memmap_outputs(self, mmap_path, shape, zarr_path, ome_path, log_label):
        """Stream a uint32 memmap label image to zarr and optionally OME-TIFF."""
        full_h, full_w = shape
        chunk_rows = 4096
        arr_ro = np.memmap(mmap_path, dtype='uint32', mode='r', shape=(full_h, full_w))
        out_z = zarr.open(
            zarr_path, mode='w',
            shape=(full_h, full_w), dtype='uint32',
            chunks=(1024, 1024),
        )
        with self.step2_profiler.time_stage("write_mask_zarr", method=self.method, output_path=self._abs(zarr_path)):
            for y in range(0, full_h, chunk_rows):
                out_z[y:y + chunk_rows, :] = arr_ro[y:y + chunk_rows, :]
        if ome_path:
            with self.step2_profiler.time_stage("export_mask_ome_tiff", method=self.method, output_path=self._abs(ome_path)):
                with tifffile.TiffWriter(ome_path, bigtiff=True) as tif:
                    tif.write(
                        arr_ro.astype(np.float32),
                        tile=(512, 512),
                        compression='lzw',
                        photometric='minisblack',
                        metadata=None,
                    )
        if self._logger:
            self._logger.info("%s → %s", log_label, ome_path or zarr_path)
        del arr_ro
        self._drop_caches()

    def _write_hq2_layer_outputs(self, layer_mmap_paths, shape, out_prefix=""):
        """Write optional HQ2 debug/proposal layers and return metadata path keys."""
        paths = {}
        if not self.write_hq2_debug_layers:
            return paths
        name_map = {
            "hq_proposal": "hq_proposal_mask_path",
            "imagej_proposal": "imagej_proposal_mask_path",
            "core": "core_mask_path",
            "expansion": "expansion_mask_path",
        }
        suffix = f"_{out_prefix}" if out_prefix else ""
        for layer_key, meta_key in name_map.items():
            mmap_path = layer_mmap_paths.get(layer_key)
            if not mmap_path:
                continue
            base = f"global_hq2_{layer_key}_mask{suffix}"
            zarr_path = os.path.join(self.output_dir, f"{base}.zarr")
            ome_path = os.path.join(self.output_dir, f"{base}.ome.tiff")
            self._write_label_memmap_outputs(
                mmap_path, shape, zarr_path,
                ome_path if self.write_hq2_debug_tiffs else "",
                f"HQ2 {layer_key}",
            )
            paths[meta_key] = self._abs(ome_path if self.write_hq2_debug_tiffs else zarr_path)
        return paths

    def _read_hq_marker_channels(self, group, channels, y0, y1, x0, x1):
        mode = str(self.seg_config.get("hq_input_mode") or "selected_channels_from_source")
        marker_channels = []
        if isinstance(group, dict) and group.get("kind") == "raw_ome":
            loader = group["loader"]
            by0, _by1, bx0, _bx1 = [0, 0, 0, 0]
            if self._current_region_bbox and len(self._current_region_bbox) == 4:
                by0, _by1, bx0, _bx1 = [int(v) for v in self._current_region_bbox]
            fy0, fy1 = by0 + int(y0), by0 + int(y1)
            fx0, fx1 = bx0 + int(x0), bx0 + int(x1)
            if mode == "step1_weighted_fusion":
                fused = None
                weights = dict(self.seg_config.get("channel_weights") or {})
                for ch in channels:
                    if getattr(self, "_channel_store", None) is not None:
                        arr = self._channel_store.read_raw_ome(loader, ch, fy0, fy1, fx0, fx1, normalize=False)
                    else:
                        arr = loader.read_region(ch, fy0, fy1, fx0, fx1, downsample=1, normalize=False)
                    arr = self._normalize01(arr) * float(weights.get(ch, 1.0))
                    fused = arr if fused is None else np.maximum(fused, arr)
                marker_channels.append(fused if fused is not None else np.zeros((y1-y0, x1-x0), dtype=np.float32))
                return marker_channels
            for ch in channels:
                if getattr(self, "_channel_store", None) is not None:
                    marker_channels.append(self._channel_store.read_raw_ome(loader, ch, fy0, fy1, fx0, fx1, normalize=False))
                else:
                    marker_channels.append(loader.read_region(ch, fy0, fy1, fx0, fx1, downsample=1, normalize=False))
            return marker_channels
        if mode == "step1_weighted_fusion":
            fused = None
            weights = dict(self.seg_config.get("channel_weights") or {})
            for ch in channels:
                if getattr(self, "_channel_store", None) is not None:
                    arr = self._channel_store.read_zarr_channel(getattr(group, "store", ""), group, ch, y0, y1, x0, x1)
                else:
                    arr = np.asarray(group[ch][y0:y1, x0:x1], dtype=np.float32)
                arr = self._normalize01(arr) * float(weights.get(ch, 1.0))
                fused = arr if fused is None else np.maximum(fused, arr)
            marker_channels.append(fused if fused is not None else np.zeros((y1-y0, x1-x0), dtype=np.float32))
            return marker_channels
        for ch in channels:
            if getattr(self, "_channel_store", None) is not None:
                arr = self._channel_store.read_zarr_channel(getattr(group, "store", ""), group, ch, y0, y1, x0, x1)
            else:
                arr = np.asarray(group[ch][y0:y1, x0:x1], dtype=np.float32)
            marker_channels.append(arr)
        return marker_channels

    def _read_mesmer_channel_source(self, group, y0, y1, x0, x1):
        nuclear = str(self.seg_config.get("nuclear_channel") or "DAPI").strip() or "DAPI"
        membrane_channels = parse_hq_channels(self.seg_config.get("membrane_channels") or [])
        requested = [nuclear] + [ch for ch in membrane_channels if ch != nuclear]
        source = {}
        if isinstance(group, dict) and group.get("kind") == "raw_ome":
            loader = group["loader"]
            by0, _by1, bx0, _bx1 = [0, 0, 0, 0]
            if self._current_region_bbox and len(self._current_region_bbox) == 4:
                by0, _by1, bx0, _bx1 = [int(v) for v in self._current_region_bbox]
            fy0, fy1 = by0 + int(y0), by0 + int(y1)
            fx0, fx1 = bx0 + int(x0), bx0 + int(x1)
            for ch in requested:
                if getattr(self, "_channel_store", None) is not None:
                    source[ch] = self._channel_store.read_raw_ome(loader, ch, fy0, fy1, fx0, fx1, normalize=False)
                else:
                    source[ch] = loader.read_region(ch, fy0, fy1, fx0, fx1, downsample=1, normalize=False)
            return source
        for ch in requested:
            if getattr(self, "_channel_store", None) is not None:
                source[ch] = self._channel_store.read_zarr_channel(getattr(group, "store", ""), group, ch, y0, y1, x0, x1)
            else:
                source[ch] = np.asarray(group[ch][y0:y1, x0:x1], dtype=np.float32)
        return source

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
        if method in (CELLPOSE_WHOLECELL_FUSION, CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION, CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2):
            if device is None:
                raise RuntimeError("PyTorch is required for Cellpose/HQ segmentation but is not available.")
            from cellpose import models as cp_models
            return {"cellpose": cp_models.CellposeModel(device=device)}
        if method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION):
            model_name = self.seg_config.get("model_name", "2D_versatile_fluo")
            model, stardist_normalize, stardist_device = load_stardist_model(
                model_name,
                prefer_gpu=self.seg_config.get("device_preference", "gpu_first") != "cpu",
            )
            if self._logger:
                self._logger.info(f"[Worker] StarDist device={stardist_device}")
            return {
                "stardist": model,
                "stardist_normalize": stardist_normalize,
                "stardist_device": stardist_device,
            }
        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            status = get_mesmer_device_status(self.seg_config.get("use_gpu", "auto"), logger=self._logger)
            if not status.mesmer_available:
                raise RuntimeError(status.error or "DeepCell/Mesmer is not installed in the current environment.")
            app = load_mesmer_application()
            if self._logger:
                self._logger.info(f"[Mesmer] device_used={status.device_used}")
            return {"mesmer": app, "mesmer_device_status": status}
        raise ValueError(f"Unknown segmentation method: {method}")

    def _segment_tile(self, tile_data, backend, hq_marker_channels=None, mesmer_channel_source=None,
                      profile_tile_id=None):
        """Return a uint32 label mask for one read tile."""
        method = self.seg_config.get("method", CELLPOSE_WHOLECELL_FUSION)
        with self.step2_profiler.time_stage("preprocess", tile_id=profile_tile_id, method=method):
            tile_f32 = tile_data.astype(np.float32) / 65535.0

        if method == CELLPOSE_WHOLECELL_FUSION:
            with self.step2_profiler.time_stage("model_inference", tile_id=profile_tile_id, method=method):
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
        if method in (CELLPOSE_NUCLEI_DAPI, CELLPOSE_NUCLEI_EXPANSION, CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2):
            with self.step2_profiler.time_stage("model_inference", tile_id=profile_tile_id, method=method):
                masks, _, _ = backend["cellpose"].eval(
                    dapi,
                    diameter=self.seg_config.get("diameter"),
                    flow_threshold=self.seg_config.get("flow_threshold", 0.4),
                    cellprob_threshold=self.seg_config.get("cellprob_threshold", 0.0),
                    min_size=self.seg_config.get("min_size", 15),
                    do_3D=False,
                )
            if method == CELLPOSE_NUCLEI_EXPANSION:
                from skimage.segmentation import expand_labels
                dist = float(self.seg_config.get("expand_distance", 8) or 0)
                if self._logger:
                    self._logger.info(f"[Step2] applying expand_labels distance={dist}")
                print(f"[Step2] applying expand_labels distance={dist}")
                with self.step2_profiler.time_stage("postprocess", tile_id=profile_tile_id, method=method):
                    if dist > 0:
                        masks = expand_labels(masks, distance=dist)
            if method == CELLPOSE_NUCLEI_HQ:
                hq_names = self.seg_config.get("hq_channels") or []
                if str(self.seg_config.get("hq_input_mode") or "") == "step1_weighted_fusion":
                    hq_names = ["step1_weighted_fusion"]
                with self.step2_profiler.time_stage("postprocess", tile_id=profile_tile_id, method=method):
                    final_mask, nuclei_mask, qc_rows = segment_nuclei_hq(
                        masks.astype(np.uint32, copy=False),
                        hq_marker_channels or [],
                        hq_names,
                        max_cell_radius=self.seg_config.get("max_cell_radius", 12),
                        normalization_low=self.seg_config.get("normalization_percentile_low", 1.0),
                        normalization_high=self.seg_config.get("normalization_percentile_high", 99.5),
                        consensus_mode=self.seg_config.get("consensus_mode", "adaptive_best_channel"),
                        channel_weights=self.seg_config.get("channel_weights") or {},
                        min_signal_threshold=self.seg_config.get("min_signal_threshold", 0.08),
                    )
                return {
                    "mask": final_mask.astype(np.uint32, copy=False),
                    "nuclei": nuclei_mask.astype(np.uint32, copy=False),
                    "qc_rows": qc_rows,
                }
            if method == CELLPOSE_NUCLEI_HQ2:
                hq_names = self.seg_config.get("hq_channels") or []
                if str(self.seg_config.get("hq_input_mode") or "") == "step1_weighted_fusion":
                    hq_names = ["step1_weighted_fusion"]
                with self.step2_profiler.time_stage("postprocess", tile_id=profile_tile_id, method=method):
                    hq2 = run_hq2_segmentation(
                        masks.astype(np.uint32, copy=False),
                        hq_marker_channels or [],
                        hq_names,
                        self.seg_config,
                        logger=self._logger,
                        return_layers=self.write_hq2_debug_layers,
                        progress_callback=lambda msg: self.progress.emit(0, 1, msg),
                        cancel_check=lambda: bool(self._stop),
                    )
                return {
                    "mask": hq2["final_labels"].astype(np.uint32, copy=False),
                    "nuclei": hq2["nuclei_labels"].astype(np.uint32, copy=False),
                    "qc_rows": hq2.get("qc_rows") or [],
                    "hq2_metadata": hq2.get("metadata") or {},
                    "hq2_layers": ({
                        "hq_proposal": hq2["hq_proposal_labels"].astype(np.uint32, copy=False),
                        "imagej_proposal": hq2["imagej_proposal_labels"].astype(np.uint32, copy=False),
                        "core": hq2["high_confidence_core_labels"].astype(np.uint32, copy=False),
                        "expansion": hq2["expansion_added_pixels"].astype(np.uint32, copy=False),
                    } if self.write_hq2_debug_layers else {}),
                }
            return masks.astype(np.uint32)

        if method in (STARDIST_NUCLEI_DAPI, STARDIST_NUCLEI_EXPANSION):
            with self.step2_profiler.time_stage("preprocess", tile_id=profile_tile_id, method=method):
                img = backend["stardist_normalize"](dapi, 1, 99.8, axis=(0, 1))
            kwargs = {}
            if self.seg_config.get("prob_thresh") is not None:
                kwargs["prob_thresh"] = self.seg_config.get("prob_thresh")
            if self.seg_config.get("nms_thresh") is not None:
                kwargs["nms_thresh"] = self.seg_config.get("nms_thresh")
            with self.step2_profiler.time_stage("model_inference", tile_id=profile_tile_id, method=method):
                masks, _ = backend["stardist"].predict_instances(img, **kwargs)
            if method == STARDIST_NUCLEI_EXPANSION:
                from skimage.segmentation import expand_labels
                dist = float(self.seg_config.get("expand_distance", 8) or 0)
                with self.step2_profiler.time_stage("postprocess", tile_id=profile_tile_id, method=method):
                    if dist > 0:
                        masks = expand_labels(masks, distance=dist)
            return masks.astype(np.uint32)

        if method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            with self.step2_profiler.time_stage("model_inference", tile_id=profile_tile_id, method=method):
                if mesmer_channel_source is not None:
                    result = run_mesmer_on_channel_source(
                        mesmer_channel_source,
                        self.seg_config,
                        app=backend.get("mesmer"),
                        logger=self._logger,
                    )
                else:
                    result = run_mesmer_on_fused_tile(
                        tile_data,
                        self.seg_config,
                        app=backend.get("mesmer"),
                        logger=self._logger,
                    )
            self.seg_config["device_used"] = result.get("device_used")
            self.seg_config["runtime_seconds_last_tile"] = result.get("runtime_seconds")
            with self.step2_profiler.time_stage("postprocess", tile_id=profile_tile_id, method=method):
                if method == MESMER_NUCLEAR_GUIDED:
                    return {
                        "mask": result["mask"].astype(np.uint32, copy=False),
                        "nuclei": result.get("nuclei"),
                        "qc_rows": [],
                    }
                return result["mask"].astype(np.uint32, copy=False)

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
        self._current_region_bbox = list(bbox) if bbox else None
        with self.step2_profiler.time_stage("load_roi", input_source=self._abs(zarr_path), method=self.method):
            z      = zarr.open(zarr_path, mode='r')
            full_h = z.shape[0]
            full_w = z.shape[1]
        log.info(f"  zarr: {full_h}×{full_w} px")

        strategy_info = self._record_tile_strategy(
            full_h,
            full_w,
            channel_count=max(2, len(parse_hq_channels(self.seg_config.get("hq_channels") or [])) or 2),
        )
        tile_h = -(-full_h // self.n_rows)
        tile_w = -(-full_w // self.n_cols)

        with self.step2_profiler.time_stage("build_tiles", method=self.method, tile_h=tile_h, tile_w=tile_w, overlap=self.overlap_px):
            tile_h, tile_w, tiles = self._build_step2_tiles(full_h, full_w)
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

        is_hq2 = self.method == CELLPOSE_NUCLEI_HQ2
        is_mesmer_guided = self.method == MESMER_NUCLEAR_GUIDED
        is_mesmer = self.method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED)
        is_hq = self.method in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2) or is_mesmer_guided
        hq_channels, hq_group = ([], None)
        mesmer_group = None
        nuclei_mmap = None
        nuclei_mmap_path = ""
        hq_qc_rows = []
        hq2_layer_mmaps = {}
        hq2_layer_mmap_paths = {}
        if is_hq and not is_mesmer_guided:
            hq_channels, hq_group = self._validate_hq_config(out_prefix)
        if is_mesmer:
            mesmer_group = self._validate_mesmer_config(out_prefix)
        if is_hq:
            nuclei_mmap_path = os.path.join(
                self.output_dir, f'global_nuclei_mask_{out_prefix}.dat'
            )
            nuclei_mmap = np.memmap(nuclei_mmap_path, dtype='uint32', mode='w+',
                                    shape=(full_h, full_w))
            nuclei_mmap[:] = 0
        if is_hq2 and self.write_hq2_debug_layers:
            for layer_key in ("hq_proposal", "imagej_proposal", "core", "expansion"):
                layer_path = os.path.join(
                    self.output_dir, f'global_hq2_{layer_key}_mask_{out_prefix}.dat'
                )
                hq2_layer_mmap_paths[layer_key] = layer_path
                hq2_layer_mmaps[layer_key] = np.memmap(
                    layer_path, dtype='uint32', mode='w+', shape=(full_h, full_w)
                )
                hq2_layer_mmaps[layer_key][:] = 0

        global_id_offset = 0
        tile_stats = []
        prefetcher = None
        if self._prefetch_enabled():
            prefetcher = TilePrefetcher(
                tiles,
                lambda idx, t: self._prepare_tile_payload(
                    zarr_path, z, t, is_hq, is_mesmer_guided, hq_group, hq_channels,
                    is_mesmer, mesmer_group, dapi_mmap=None, profile_tile_base=None,
                ),
                prefetch_queue_size=int(self.seg_config.get("prefetch_queue_size", 2) or 2),
                logger=log,
                profiler=self.step2_profiler,
            )
            log.info("[TilePrefetch] enabled queue_size=%s", prefetcher.prefetch_queue_size)

        with self.step2_profiler.time_stage("run_all_tiles", method=self.method, input_source=self._abs(zarr_path)):
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

            profile_tile_id = f"{out_prefix}:{i}" if out_prefix else str(i)
            stage_seconds = {}
            tile_shape = (ry1 - ry0, rx1 - rx0)
            tile_profile_base = {
                "tile_id": profile_tile_id,
                "bbox_global": list(bbox) if bbox else [oy0, oy1, ox0, ox1],
                "bbox_local": [oy0, oy1, ox0, ox1],
                "tile_shape": list(tile_shape),
                "tile_h": int(tile_shape[0]),
                "tile_w": int(tile_shape[1]),
                "overlap": int(self.overlap_px),
                "channels_used": list(hq_channels or []),
                "method": self.method,
                "dtype": str(getattr(z, "dtype", "")),
                "input_source": self._abs(zarr_path),
            }
            self.step2_profiler.record_tile_metadata(profile_tile_id, row=row, col=col, **tile_profile_base)

            _t = time.perf_counter()
            if prefetcher is not None:
                payload = prefetcher.get(
                    i,
                    sync_load_fn=lambda idx, t: self._prepare_tile_payload(
                        zarr_path, z, t, is_hq, is_mesmer_guided, hq_group, hq_channels,
                        is_mesmer, mesmer_group, dapi_mmap=None, profile_tile_base=tile_profile_base,
                    ),
                )
                tile_data = payload["tile_data"]
                dapi_own = payload["dapi_own"]
                hq_marker_channels_prefetched = payload.get("hq_marker_channels")
                mesmer_channel_source_prefetched = payload.get("mesmer_channel_source")
                dapi_mmap[oy0:oy1, ox0:ox1] = dapi_own[:own_h, :own_w]
            else:
                with self.step2_profiler.time_stage("read_tile", **tile_profile_base):
                    payload = self._prepare_tile_payload(
                        zarr_path, z, tile, is_hq, is_mesmer_guided, hq_group, hq_channels,
                        is_mesmer, mesmer_group, dapi_mmap=dapi_mmap, profile_tile_base=None,
                    )
                    tile_data = payload["tile_data"]
                    dapi_own = payload["dapi_own"]
                    hq_marker_channels_prefetched = payload.get("hq_marker_channels")
                    mesmer_channel_source_prefetched = payload.get("mesmer_channel_source")
            stage_seconds["read_tile"] = time.perf_counter() - _t

            if self.recovery_npy_dir is not None:
                local_nuclei = None
                local_qc_rows = []
                local_hq2_layers = {}
                local_hq2_metadata = {}
                npy_path = os.path.join(
                    self.recovery_npy_dir,
                    f'tile_{out_prefix}_{row}_{col}.npy'
                )
                if not os.path.exists(npy_path):
                    log.warning(f"  Missing: {npy_path}, skipping")
                    del tile_data
                    continue
                _t = time.perf_counter()
                with self.step2_profiler.time_stage("model_inference", **tile_profile_base):
                    local_mask = np.load(npy_path)
                stage_seconds["model_inference"] = time.perf_counter() - _t
            else:
                try:
                    hq_marker_channels = hq_marker_channels_prefetched
                    mesmer_channel_source = mesmer_channel_source_prefetched
                    _t = time.perf_counter()
                    if hq_marker_channels is None and mesmer_channel_source is None:
                        with self.step2_profiler.time_stage("read_tile", **tile_profile_base):
                            if is_hq and not is_mesmer_guided:
                                hq_marker_channels = self._read_hq_marker_channels(
                                    hq_group, hq_channels, ry0, ry1, rx0, rx1
                                )
                            if is_mesmer and mesmer_group is not None:
                                mesmer_channel_source = self._read_mesmer_channel_source(
                                    mesmer_group, ry0, ry1, rx0, rx1
                                )
                        stage_seconds["read_tile"] += time.perf_counter() - _t
                    local_result = self._segment_tile(
                        tile_data,
                        model,
                        hq_marker_channels,
                        mesmer_channel_source=mesmer_channel_source,
                        profile_tile_id=profile_tile_id,
                    )
                    local_nuclei = None
                    local_qc_rows = []
                    local_hq2_layers = {}
                    local_hq2_metadata = {}
                    if isinstance(local_result, dict):
                        local_mask = local_result["mask"]
                        local_nuclei = local_result.get("nuclei")
                        local_qc_rows = local_result.get("qc_rows") or []
                        local_hq2_layers = local_result.get("hq2_layers") or {}
                        local_hq2_metadata = local_result.get("hq2_metadata") or {}
                    else:
                        local_mask = local_result
                except Exception as e:
                    log.error(f"  Tile [{row},{col}] failed: {traceback.format_exc()}")
                    local_mask = np.zeros((ry1-ry0, rx1-rx0), dtype=np.uint32)
                    local_nuclei = None
                    local_qc_rows = []
                    local_hq2_layers = {}
                    local_hq2_metadata = {}
                if use_gpu:
                    self._empty_torch_cache_if_available()
                self.step2_profiler.log_tile_stage(
                    profile_tile_id,
                    "inference_wait",
                    0.0,
                    **self._prof_metrics(tile_profile_base),
                )

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
            _t = time.perf_counter()
            with self.step2_profiler.time_stage("postprocess", **tile_profile_base):
                raw_own_mask = local_mask[local_oy0:local_oy1,
                                          local_ox0:local_ox1].copy()
            stage_seconds["postprocess"] = stage_seconds.get("postprocess", 0.0) + (time.perf_counter() - _t)

            dapi_tile_path = ""
            raw_mask_tile_path = ""
            if self.write_tile_tiffs:
                dapi_tile_path = os.path.join(
                    tile_dir, f'tile_r{row}_c{col}_dapi.ome.tiff'
                )
                _t = time.perf_counter()
                with self.step2_profiler.time_stage("merge_or_write", output_path=self._abs(dapi_tile_path), **tile_profile_base):
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
                stage_seconds["merge_or_write"] = stage_seconds.get("merge_or_write", 0.0) + (time.perf_counter() - _t)

                raw_mask_tile_path = os.path.join(
                    tile_dir, f'tile_r{row}_c{col}_raw_mask.ome.tiff'
                )
                _t = time.perf_counter()
                with self.step2_profiler.time_stage("merge_or_write", output_path=self._abs(raw_mask_tile_path), **tile_profile_base):
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
                stage_seconds["merge_or_write"] = stage_seconds.get("merge_or_write", 0.0) + (time.perf_counter() - _t)
            del raw_own_mask

            n_raw = int(local_mask.max())
            if n_raw == 0:
                self.step2_profiler.record_tile_metadata(profile_tile_id, labels_count=0, output_path=self._abs(raw_mask_tile_path))
                stage_seconds["_profile_tile_id"] = profile_tile_id
                self._profile_tile_line(i, n_tiles, stage_seconds, 0, tile_shape)
                self.tile_done.emit(i, n_tiles, 0)
                del local_mask
                gc.collect()
                self._drop_caches()
                continue

            _t = time.perf_counter()
            with self.step2_profiler.time_stage("postprocess", labels_count=n_raw, **tile_profile_base):
                cy, cx = self._centroids_vectorised(local_mask)
            stage_seconds["postprocess"] = stage_seconds.get("postprocess", 0.0) + (time.perf_counter() - _t)

            keep_labels = []
            _t = time.perf_counter()
            with self.step2_profiler.time_stage("relabel", labels_count=n_raw, **tile_profile_base):
                for label_idx in range(n_raw):
                    lcy, lcx = cy[label_idx], cx[label_idx]
                    if (lcy >= local_oy0 and lcy < local_oy1 and
                            lcx >= local_ox0 and lcx < local_ox1):
                        keep_labels.append(label_idx + 1)

                if not keep_labels:
                    pass
                else:
                    lut = np.zeros(n_raw + 1, dtype=np.uint32)
                    for new_id, lab in enumerate(keep_labels, start=1):
                        lut[lab] = new_id + global_id_offset

                    remapped = lut[local_mask]
                    remapped_nuclei = None
                    if is_hq and local_nuclei is not None:
                        safe_nuclei = np.where(local_nuclei <= n_raw, local_nuclei, 0).astype(np.uint32, copy=False)
                        remapped_nuclei = lut[safe_nuclei]
                    remapped_hq2_layers = {}
                    if is_hq2 and local_hq2_layers:
                        for layer_key, layer_arr in local_hq2_layers.items():
                            layer_arr = np.asarray(layer_arr, dtype=np.uint32)
                            safe = np.where(layer_arr <= n_raw, layer_arr, 0).astype(np.uint32, copy=False)
                            remapped_hq2_layers[layer_key] = lut[safe]
                    if is_hq and local_qc_rows:
                        kept_set = set(keep_labels)
                        for row_qc in local_qc_rows:
                            old_id = int(row_qc.get("cell_id", 0) or 0)
                            if old_id not in kept_set:
                                continue
                            new_row = dict(row_qc)
                            new_row["cell_id"] = int(lut[old_id])
                            hq_qc_rows.append(new_row)
                    if is_hq2 and local_hq2_metadata:
                        tile_meta = dict(local_hq2_metadata)
                        tile_meta.update({"row": row, "col": col, "out_prefix": out_prefix})
                        self._hq2_tile_metadata.append(tile_meta)
            stage_seconds["relabel"] = stage_seconds.get("relabel", 0.0) + (time.perf_counter() - _t)

            if not keep_labels:
                self.step2_profiler.record_tile_metadata(profile_tile_id, labels_count=0, output_path=self._abs(raw_mask_tile_path))
                stage_seconds["_profile_tile_id"] = profile_tile_id
                self._profile_tile_line(i, n_tiles, stage_seconds, 0, tile_shape)
                self.tile_done.emit(i, n_tiles, 0)
                del local_mask, cy, cx
                gc.collect()
                self._drop_caches()
                continue
            del local_mask, cy, cx

            _t = time.perf_counter()
            with self.step2_profiler.time_stage("merge_or_write", labels_count=len(keep_labels), **tile_profile_base):
                dst = mmap[ry0:ry1, rx0:rx1]
                np.copyto(dst, remapped, where=(remapped > 0))
                del remapped
                if is_hq and nuclei_mmap is not None and remapped_nuclei is not None:
                    ndst = nuclei_mmap[ry0:ry1, rx0:rx1]
                    np.copyto(ndst, remapped_nuclei, where=(remapped_nuclei > 0))
                    del remapped_nuclei
                if is_hq2 and remapped_hq2_layers:
                    for layer_key, layer_arr in remapped_hq2_layers.items():
                        layer_mmap = hq2_layer_mmaps.get(layer_key)
                        if layer_mmap is None:
                            continue
                        ldst = layer_mmap[ry0:ry1, rx0:rx1]
                        np.copyto(ldst, layer_arr, where=(layer_arr > 0))
                    del remapped_hq2_layers
            stage_seconds["merge_or_write"] = stage_seconds.get("merge_or_write", 0.0) + (time.perf_counter() - _t)
            self.step2_profiler.log_tile_stage(
                profile_tile_id,
                "tile_write",
                stage_seconds.get("merge_or_write", 0.0),
                **self._prof_metrics(tile_profile_base),
            )

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
            self.step2_profiler.record_tile_metadata(
                profile_tile_id,
                labels_count=n_kept,
                output_path=self._abs(raw_mask_tile_path or mmap_path),
            )

            self.tile_done.emit(i, n_tiles, n_kept)
            log.info(f"  ✓ [{out_prefix}] Tile [{i+1}/{n_tiles}] kept={n_kept}")
            stage_seconds["_profile_tile_id"] = profile_tile_id
            self._profile_tile_line(i, n_tiles, stage_seconds, n_kept, tile_shape)
            gc.collect()
            self._drop_caches()
        if prefetcher is not None:
            metrics = prefetcher.snapshot_metrics()
            self.step2_profiler.log_tile_stage(None, "tile_prefetch_wait", metrics.get("prefetch_wait_seconds", 0.0), **metrics)
            prefetcher.close()

        # ── Flush memmaps ─────────────────────────────────────────────
        with self.step2_profiler.time_stage("merge_all_tiles", method=self.method, output_path=self._abs(mmap_path)):
            mmap.flush()
            dapi_mmap.flush()
            if nuclei_mmap is not None:
                nuclei_mmap.flush()
            for layer_mmap in hq2_layer_mmaps.values():
                layer_mmap.flush()
        total_cells = int(global_id_offset)
        log.info(f"  [{out_prefix}] total_cells={total_cells:,}")

        del mmap, dapi_mmap
        if nuclei_mmap is not None:
            del nuclei_mmap
        for layer_key in list(hq2_layer_mmaps.keys()):
            del hq2_layer_mmaps[layer_key]
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
        nuclei_mmap_ro = None
        if is_hq and nuclei_mmap_path:
            nuclei_mmap_ro = np.memmap(nuclei_mmap_path, dtype='uint32', mode='r',
                                       shape=(full_h, full_w))

        out_zarr_path = os.path.join(
            self.output_dir, f'global_mask_{out_prefix}.zarr'
        )
        out_z = zarr.open(
            out_zarr_path, mode='w',
            shape=(full_h, full_w), dtype='uint32',
            chunks=(1024, 1024),
        )
        with self.step2_profiler.time_stage("write_mask_zarr", method=self.method, output_path=self._abs(out_zarr_path)):
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
        with self.step2_profiler.time_stage("export_mask_ome_tiff", method=self.method, output_path=self._abs(ome_path)):
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
        with self.step2_profiler.time_stage("export_ome_tiff", method=self.method, output_path=self._abs(global_dapi_path)):
            with tifffile.TiffWriter(global_dapi_path, bigtiff=True) as tif:
                tif.write(
                    np.array(dapi_mmap_ro),
                    tile=(512, 512),
                    compression='lzw',
                    photometric='minisblack',
                    metadata=None,
                )
        self._drop_caches()

        nuclei_ome_path = ""
        nuclei_zarr_path = ""
        qc_table_path = ""
        if is_hq and nuclei_mmap_ro is not None:
            nuclei_zarr_path = os.path.join(
                self.output_dir, f'global_nuclei_mask_{out_prefix}.zarr'
            )
            nz = zarr.open(nuclei_zarr_path, mode='w',
                           shape=(full_h, full_w), dtype='uint32',
                           chunks=(1024, 1024))
            with self.step2_profiler.time_stage("write_mask_zarr", method=self.method, output_path=self._abs(nuclei_zarr_path)):
                for y in range(0, full_h, CHUNK):
                    nz[y:y+CHUNK, :] = nuclei_mmap_ro[y:y+CHUNK, :]
            nuclei_ome_path = os.path.join(
                self.output_dir, f'global_nuclei_mask_{out_prefix}.ome.tiff'
            )
            with self.step2_profiler.time_stage("export_mask_ome_tiff", method=self.method, output_path=self._abs(nuclei_ome_path)):
                with tifffile.TiffWriter(nuclei_ome_path, bigtiff=True) as tif:
                    tif.write(
                        nuclei_mmap_ro.astype(np.float32),
                        tile=(512, 512),
                        compression='lzw',
                        photometric='minisblack',
                        metadata=None,
                    )
            if is_mesmer_guided:
                qc_table_path = ""
            elif is_hq2:
                qc_table_path = os.path.join(self.output_dir, f'hq2_qc_table_{out_prefix}.csv')
                with self.step2_profiler.time_stage("metadata_write", method=self.method, output_path=self._abs(qc_table_path)):
                    write_hq2_qc_table(qc_table_path, hq_qc_rows)
            else:
                qc_table_path = os.path.join(self.output_dir, f'hq_qc_table_{out_prefix}.csv')
                with self.step2_profiler.time_stage("metadata_write", method=self.method, output_path=self._abs(qc_table_path)):
                    write_hq_qc_table(qc_table_path, hq_qc_rows)
            del nuclei_mmap_ro

        hq2_paths = {}
        if is_hq2:
            hq2_paths = self._write_hq2_layer_outputs(
                hq2_layer_mmap_paths, (full_h, full_w), out_prefix=out_prefix
            )

        del mmap_ro, dapi_mmap_ro
        gc.collect()

        meta = {
            'mode':            'roi',
            'run_id':          self.result_id,
            'roi_id':          self.roi_id,
            'roi_display_name': out_prefix,
            'method':          self.method,
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
            'tile_strategy':     self._step2_engine_meta(),
            'tile_strategy_mode': self._tile_strategy_info.get("tile_strategy_mode", "manual"),
            'suggested_tile_h':  self._tile_strategy_info.get("suggested_tile_h"),
            'suggested_tile_w':  self._tile_strategy_info.get("suggested_tile_w"),
            'actual_tile_h':     self._tile_strategy_info.get("actual_tile_h"),
            'actual_tile_w':     self._tile_strategy_info.get("actual_tile_w"),
            'estimated_tile_mpx': self._tile_strategy_info.get("estimated_tile_mpx"),
            'tile_stats':      tile_stats,
            'seg_config':      self.seg_config,
            'cp_params':       self.seg_config,
            'bbox':            list(bbox) if bbox else None,
            'created_at':      datetime.now().isoformat(),
        }
        if self.method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
            meta.update(mesmer_metadata(
                self.method,
                self.seg_config,
                getattr(model, "get", lambda _k, _d=None: _d)("mesmer_device_status") if isinstance(model, dict) else None,
                output_mask_path=self._abs(ome_path),
                extra={
                    "nuclei_mask_path": self._abs(nuclei_ome_path),
                    "whole_cell_mask_path": self._abs(ome_path) if self.method != MESMER_NUCLEI else "",
                },
            ))
        if is_hq2:
            hq2_meta_paths = dict(hq2_paths)
            hq2_meta_paths.update({
                "nuclei_mask_path": self._abs(nuclei_ome_path),
                "final_cell_mask_path": self._abs(ome_path),
                "qc_table_path": self._abs(qc_table_path),
            })
            meta.update(hq2_metadata_fields(self.seg_config, hq2_meta_paths))
            meta["hq2_tile_metadata"] = list(self._hq2_tile_metadata)
        elif is_hq:
            meta.update(self._hq_meta_fields(nuclei_ome_path, ome_path, qc_table_path))
        if self.roi_id:
            mask_roi_id = os.path.join(self.output_dir, f"global_mask_{self.roi_id}.ome.tiff")
            dapi_roi_id = os.path.join(self.output_dir, f"global_dapi_{self.roi_id}.ome.tiff")
            try:
                self._make_alias(ome_path, os.path.basename(mask_roi_id))
                self._make_alias(global_dapi_path, os.path.basename(dapi_roi_id))
            except Exception as e:
                log.warning(f"Could not create roi_id OME aliases: {e}")
            meta["paths"] = {
                "dapi_ome": self._abs(dapi_roi_id if os.path.exists(dapi_roi_id) or os.path.islink(dapi_roi_id) else global_dapi_path),
                "mask_ome": self._abs(mask_roi_id if os.path.exists(mask_roi_id) or os.path.islink(mask_roi_id) else ome_path),
                "mask_zarr": self._abs(out_zarr_path),
                "fusion_zarr": self._abs(zarr_path),
                "corrected_channels_zarr": self._multichannel_source_path(),
                "raw_ome": self._abs(self.roi_manifest.get("source_ome") or ""),
            }
            meta["roi_bbox_fullres"] = list(bbox) if bbox else self.roi_manifest.get("bbox_fullres")
            meta["roi_shape"] = meta.get("image_shape")
        meta_path = os.path.join(self.output_dir, f'segmentation_meta_{out_prefix}.json')
        with self.step2_profiler.time_stage("write_segmentation_meta", method=self.method, output_path=self._abs(meta_path)):
            with open(meta_path, 'w') as f:
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
            self._logger, log_path = self._setup_logger()
            log = self._logger
            log.info("=== Segmentation started ===")
            register_legacy_result(self.project_output_dir)
            config_path = self._write_run_segmentation_config()
            log.info(f"run_segmentation_params.json -> {config_path}")

            try:
                import psutil
            except ImportError:
                log.warning(
                    "psutil not installed — RAM usage cannot be monitored. "
                    "Run: pip install psutil"
                )
                print("[WARNING] pip install psutil  — RAM monitoring disabled")

            log.info(f"Initial memory: {self._mem_snapshot()}")
            self._start_runtime_monitor(interval_s=2)

            if self.recovery_npy_dir is None:
                if self._uses_torch_backend():
                    try:
                        import torch
                    except ImportError as exc:
                        raise RuntimeError(
                            "PyTorch is required for Cellpose/HQ Step2 segmentation, "
                            "but it is not installed in the current environment."
                        ) from exc
                    use_gpu = torch.cuda.is_available()
                    device = torch.device('cuda' if use_gpu else 'cpu')
                else:
                    use_gpu = str(self.seg_config.get("use_gpu", "auto")).lower() != "cpu"
                    device = None
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
                    roi_zarr = self._fusion_source_path(roi_name)
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
                        "roi_id": self.roi_id,
                        "roi_display_name": roi_name,
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
                        "paths": region_meta.get("paths") or {},
                        "roi_bbox_fullres": region_meta.get("roi_bbox_fullres"),
                        "roi_shape": region_meta.get("roi_shape"),
                    })
                    self.progress.emit(
                        roi_i + 1, len(self.rois),
                        f"✓ ROI {roi_name}: {n_cells:,} cells  "
                        f"(cumulative: {total_cells_all:,})"
                    )

                if model is not None:
                    del model
                    gc.collect()
                    if use_gpu:
                        self._empty_torch_cache_if_available()
                    self._drop_caches()

                log.info(
                    f"=== All ROIs done  total_cells={total_cells_all:,} ==="
                )
                self._write_roi_canonical_aliases(roi_meta_all)
                first_roi_meta = roi_meta_all[0] if roi_meta_all else {}
                first_paths = dict(first_roi_meta.get("paths") or {})
                summary_meta = {
                    "mode": "roi",
                    "run_id": self.result_id,
                    "roi_id": self.roi_id,
                    "roi_display_name": self.roi_display_name,
                    "method": self.method,
                    "created_at": self.created_at,
                    "roi_bbox_fullres": self.roi_manifest.get("bbox_fullres") or first_roi_meta.get("bbox_fullres"),
                    "roi_shape": self.roi_manifest.get("shape") or first_roi_meta.get("image_shape"),
                    "paths": {
                        "dapi_ome": first_paths.get("dapi_ome") or first_roi_meta.get("global_dapi") or "",
                        "mask_ome": first_paths.get("mask_ome") or first_roi_meta.get("ome_tiff") or "",
                        "mask_zarr": first_paths.get("mask_zarr") or first_roi_meta.get("zarr_path") or "",
                        "fusion_zarr": first_paths.get("fusion_zarr") or first_roi_meta.get("fused_zarr_path") or self._fusion_source_path(self.roi_display_name),
                        "corrected_channels_zarr": first_paths.get("corrected_channels_zarr") or self._multichannel_source_path(),
                        "raw_ome": first_paths.get("raw_ome") or self._abs(self.roi_manifest.get("source_ome") or ""),
                    },
                    "output_dir": self._abs(self.output_dir),
                    "project_output_dir": self._abs(self.project_output_dir),
                    "result_id": self.result_id,
                    "display_name": self.seg_config.get("display_name", self.method),
                    "rois": roi_meta_all,
                    "total_cells": total_cells_all,
                    "seg_config": self.seg_config,
                    "config_path": self._abs(config_path),
                    "tile_strategy": self._step2_engine_meta(),
                }
                if self.method == CELLPOSE_NUCLEI_HQ2:
                    summary_meta.update(hq2_metadata_fields(self.seg_config, {
                        "nuclei_mask_path": first_roi_meta.get("nuclei_mask_path", ""),
                        "hq_proposal_mask_path": first_roi_meta.get("hq_proposal_mask_path", ""),
                        "imagej_proposal_mask_path": first_roi_meta.get("imagej_proposal_mask_path", ""),
                        "core_mask_path": first_roi_meta.get("core_mask_path", ""),
                        "expansion_mask_path": first_roi_meta.get("expansion_mask_path", ""),
                        "final_cell_mask_path": first_roi_meta.get("final_cell_mask_path") or first_roi_meta.get("ome_tiff", ""),
                        "qc_table_path": first_roi_meta.get("qc_table_path", ""),
                    }))
                    summary_meta["hq2_tile_metadata"] = list(self._hq2_tile_metadata)
                elif self.method == CELLPOSE_NUCLEI_HQ:
                    summary_meta.update(self._hq_meta_fields(
                        first_roi_meta.get("nuclei_mask_path", ""),
                        first_roi_meta.get("final_cell_mask_path") or first_roi_meta.get("ome_tiff", ""),
                        first_roi_meta.get("qc_table_path", ""),
                    ))
                runtime_meta = self._finish_runtime_monitor()
                summary_meta["runtime"] = runtime_meta
                summary_meta_path = os.path.join(self.output_dir, "segmentation_meta.json")
                with self.step2_profiler.time_stage("write_segmentation_meta", method=self.method, output_path=self._abs(summary_meta_path)):
                    with open(summary_meta_path, "w") as f:
                        json.dump(summary_meta, f, indent=2)
                self._register_completed_result(summary_meta)
                if self.roi_dir and self.roi_id:
                    rel_run_path = os.path.relpath(self.output_dir, self.roi_dir)
                    update_roi_segmentation_run(self.roi_dir, {
                        "run_id": self.result_id,
                        "method": self.method,
                        "created_at": self.created_at,
                        "path": rel_run_path,
                        "status": "done",
                        "meta_path": os.path.join(rel_run_path, "segmentation_meta.json"),
                    })
                    print(f"[Step2] run_id={self.result_id}")
                    print(f"[Step2] roi_id={self.roi_id}")
                    print(f"[Step2] method={self.method}")
                    print(f"[Step2] output_run_dir={self.output_dir}")
                    print(f"[Step2] dapi_ome={summary_meta['paths']['dapi_ome']}")
                    print(f"[Step2] mask_ome={summary_meta['paths']['mask_ome']}")
                    print(f"[Step2] fusion_zarr={summary_meta['paths'].get('fusion_zarr')}")
                    print("[Step2] updated roi_index latest_by_method")
                self._record_engine_metrics()
                profile_summary = self.step2_profiler.finalize()
                self._profile_summary(profile_summary)
                if getattr(self, "_channel_store", None) is not None:
                    self._channel_store.close()
                self.finished.emit(self.output_dir, total_cells_all)
                return

            # ── Full WSI mode ─────────────────────────────────────────
            with self.step2_profiler.time_stage("load_roi", input_source=self._abs(self.zarr_path), method=self.method):
                z       = zarr.open(self.zarr_path, mode='r')
                full_h  = z.shape[0]
                full_w  = z.shape[1]
            log.info(f"Input zarr: {full_h}×{full_w} px")

            strategy_info = self._record_tile_strategy(
                full_h,
                full_w,
                channel_count=max(2, len(parse_hq_channels(self.seg_config.get("hq_channels") or [])) or 2),
            )
            tile_h = -(-full_h // self.n_rows)
            tile_w = -(-full_w // self.n_cols)

            with self.step2_profiler.time_stage("build_tiles", method=self.method, tile_h=tile_h, tile_w=tile_w, overlap=self.overlap_px):
                tile_h, tile_w, tiles = self._build_step2_tiles(full_h, full_w)
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

            is_hq2 = self.method == CELLPOSE_NUCLEI_HQ2
            is_mesmer_guided = self.method == MESMER_NUCLEAR_GUIDED
            is_mesmer = self.method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED)
            is_hq = self.method in (CELLPOSE_NUCLEI_HQ, CELLPOSE_NUCLEI_HQ2) or is_mesmer_guided
            hq_channels, hq_group = ([], None)
            mesmer_group = None
            nuclei_mmap = None
            nuclei_mmap_path = ""
            hq_qc_rows = []
            hq2_layer_mmaps = {}
            hq2_layer_mmap_paths = {}
            if is_hq and not is_mesmer_guided:
                hq_channels, hq_group = self._validate_hq_config()
            if is_mesmer:
                mesmer_group = self._validate_mesmer_config()
            if is_hq:
                nuclei_mmap_path = os.path.join(self.output_dir, 'global_nuclei_mask.dat')
                nuclei_mmap = np.memmap(nuclei_mmap_path, dtype='uint32', mode='w+',
                                        shape=(full_h, full_w))
                nuclei_mmap[:] = 0
            if is_hq2 and self.write_hq2_debug_layers:
                for layer_key in ("hq_proposal", "imagej_proposal", "core", "expansion"):
                    layer_path = os.path.join(self.output_dir, f'global_hq2_{layer_key}_mask.dat')
                    hq2_layer_mmap_paths[layer_key] = layer_path
                    hq2_layer_mmaps[layer_key] = np.memmap(
                        layer_path, dtype='uint32', mode='w+', shape=(full_h, full_w)
                    )
                    hq2_layer_mmaps[layer_key][:] = 0

            global_id_offset = 0
            tile_stats = []
            prefetcher = None
            if self._prefetch_enabled():
                prefetcher = TilePrefetcher(
                    tiles,
                    lambda idx, t: self._prepare_tile_payload(
                        self.zarr_path, z, t, is_hq, is_mesmer_guided, hq_group, hq_channels,
                        is_mesmer, mesmer_group, dapi_mmap=None, profile_tile_base=None,
                    ),
                    prefetch_queue_size=int(self.seg_config.get("prefetch_queue_size", 2) or 2),
                    logger=log,
                    profiler=self.step2_profiler,
                )
                log.info("[TilePrefetch] enabled queue_size=%s", prefetcher.prefetch_queue_size)

            with self.step2_profiler.time_stage("run_all_tiles", method=self.method, input_source=self._abs(self.zarr_path)):
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

                profile_tile_id = str(i)
                stage_seconds = {}
                tile_shape = (ry1 - ry0, rx1 - rx0)
                tile_profile_base = {
                    "tile_id": profile_tile_id,
                    "bbox_global": [oy0, oy1, ox0, ox1],
                    "bbox_local": [oy0, oy1, ox0, ox1],
                    "tile_shape": list(tile_shape),
                    "tile_h": int(tile_shape[0]),
                    "tile_w": int(tile_shape[1]),
                    "overlap": int(self.overlap_px),
                    "channels_used": list(hq_channels or []),
                    "method": self.method,
                    "dtype": str(getattr(z, "dtype", "")),
                    "input_source": self._abs(self.zarr_path),
                }
                self.step2_profiler.record_tile_metadata(profile_tile_id, row=row, col=col, **tile_profile_base)

                _t = time.perf_counter()
                if prefetcher is not None:
                    payload = prefetcher.get(
                        i,
                        sync_load_fn=lambda idx, t: self._prepare_tile_payload(
                            self.zarr_path, z, t, is_hq, is_mesmer_guided, hq_group, hq_channels,
                            is_mesmer, mesmer_group, dapi_mmap=None, profile_tile_base=tile_profile_base,
                        ),
                    )
                    tile_data = payload["tile_data"]
                    dapi_own = payload["dapi_own"]
                    hq_marker_channels_prefetched = payload.get("hq_marker_channels")
                    mesmer_channel_source_prefetched = payload.get("mesmer_channel_source")
                    dapi_mmap[oy0:oy1, ox0:ox1] = dapi_own[:own_h, :own_w]
                else:
                    with self.step2_profiler.time_stage("read_tile", **tile_profile_base):
                        payload = self._prepare_tile_payload(
                            self.zarr_path, z, tile, is_hq, is_mesmer_guided, hq_group, hq_channels,
                            is_mesmer, mesmer_group, dapi_mmap=dapi_mmap, profile_tile_base=None,
                        )
                        tile_data = payload["tile_data"]
                        dapi_own = payload["dapi_own"]
                        hq_marker_channels_prefetched = payload.get("hq_marker_channels")
                        mesmer_channel_source_prefetched = payload.get("mesmer_channel_source")
                stage_seconds["read_tile"] = time.perf_counter() - _t

                if self.recovery_npy_dir is not None:
                    local_nuclei = None
                    local_qc_rows = []
                    local_hq2_layers = {}
                    local_hq2_metadata = {}
                    npy_path = os.path.join(
                        self.recovery_npy_dir, f'tile_{row}_{col}.npy'
                    )
                    if not os.path.exists(npy_path):
                        self.progress.emit(
                            i, n_tiles,
                            f'  ⚠ {npy_path} not found, skipping'
                        )
                        continue
                    _t = time.perf_counter()
                    with self.step2_profiler.time_stage("model_inference", **tile_profile_base):
                        local_mask = np.load(npy_path)
                    stage_seconds["model_inference"] = time.perf_counter() - _t
                else:
                    try:
                        hq_marker_channels = hq_marker_channels_prefetched
                        mesmer_channel_source = mesmer_channel_source_prefetched
                        _t = time.perf_counter()
                        if hq_marker_channels is None and mesmer_channel_source is None:
                            with self.step2_profiler.time_stage("read_tile", **tile_profile_base):
                                if is_hq and not is_mesmer_guided:
                                    hq_marker_channels = self._read_hq_marker_channels(
                                        hq_group, hq_channels, ry0, ry1, rx0, rx1
                                    )
                                if is_mesmer and mesmer_group is not None:
                                    mesmer_channel_source = self._read_mesmer_channel_source(
                                        mesmer_group, ry0, ry1, rx0, rx1
                                    )
                            stage_seconds["read_tile"] += time.perf_counter() - _t
                        local_result = self._segment_tile(
                            tile_data,
                            model,
                            hq_marker_channels,
                            mesmer_channel_source=mesmer_channel_source,
                            profile_tile_id=profile_tile_id,
                        )
                        local_nuclei = None
                        local_qc_rows = []
                        local_hq2_layers = {}
                        local_hq2_metadata = {}
                        if isinstance(local_result, dict):
                            local_mask = local_result["mask"]
                            local_nuclei = local_result.get("nuclei")
                            local_qc_rows = local_result.get("qc_rows") or []
                            local_hq2_layers = local_result.get("hq2_layers") or {}
                            local_hq2_metadata = local_result.get("hq2_metadata") or {}
                        else:
                            local_mask = local_result
                    except Exception as e:
                        log.error(f'Tile [{row},{col}] inference failed:\n{traceback.format_exc()}')
                        self.error.emit(f'Tile [{row},{col}] inference failed: {e}')
                        local_mask = np.zeros((ry1-ry0, rx1-rx0), dtype=np.uint32)
                        local_nuclei = None
                        local_qc_rows = []
                        local_hq2_layers = {}
                        local_hq2_metadata = {}
                    del tile_data
                    if use_gpu:
                        self._empty_torch_cache_if_available()
                    self.step2_profiler.log_tile_stage(
                        profile_tile_id,
                        "inference_wait",
                        0.0,
                        **self._prof_metrics(tile_profile_base),
                    )

                local_oy0 = oy0 - ry0
                local_oy1 = oy1 - ry0
                local_ox0 = ox0 - rx0
                local_ox1 = ox1 - rx0
                _t = time.perf_counter()
                with self.step2_profiler.time_stage("postprocess", **tile_profile_base):
                    raw_own_mask = local_mask[local_oy0:local_oy1,
                                              local_ox0:local_ox1].copy()
                stage_seconds["postprocess"] = stage_seconds.get("postprocess", 0.0) + (time.perf_counter() - _t)

                dapi_tile_path = ""
                raw_mask_tile_path = ""
                if self.write_tile_tiffs:
                    dapi_tile_path = os.path.join(
                        tile_dir, f'tile_r{row}_c{col}_dapi.ome.tiff'
                    )
                    _t = time.perf_counter()
                    with self.step2_profiler.time_stage("merge_or_write", output_path=self._abs(dapi_tile_path), **tile_profile_base):
                        try:
                            self._write_tile_ometiff(
                                dapi_tile_path,
                                dapi_own[:own_h, :own_w].astype(np.uint16),
                                description=f'DAPI row={row} col={col} '
                                            f'own=({oy0},{oy1},{ox0},{ox1})',
                            )
                        except Exception as e:
                            log.warning(f"  dapi tile write failed: {e}")
                    stage_seconds["merge_or_write"] = stage_seconds.get("merge_or_write", 0.0) + (time.perf_counter() - _t)

                    raw_mask_tile_path = os.path.join(
                        tile_dir, f'tile_r{row}_c{col}_raw_mask.ome.tiff'
                    )
                    _t = time.perf_counter()
                    with self.step2_profiler.time_stage("merge_or_write", output_path=self._abs(raw_mask_tile_path), **tile_profile_base):
                        try:
                            self._write_tile_ometiff(
                                raw_mask_tile_path,
                                raw_own_mask.astype(np.float32),
                                description=f'raw mask row={row} col={col} '
                                            f'n_cells={int(raw_own_mask.max())}',
                            )
                        except Exception as e:
                            log.warning(f"  raw mask tile write failed: {e}")
                    stage_seconds["merge_or_write"] = stage_seconds.get("merge_or_write", 0.0) + (time.perf_counter() - _t)
                del raw_own_mask, dapi_own

                n_raw = int(local_mask.max())
                if n_raw == 0:
                    self.step2_profiler.record_tile_metadata(profile_tile_id, labels_count=0, output_path=self._abs(raw_mask_tile_path))
                    stage_seconds["_profile_tile_id"] = profile_tile_id
                    self._profile_tile_line(i, n_tiles, stage_seconds, 0, tile_shape)
                    self.tile_done.emit(i, n_tiles, 0)
                    del local_mask
                    gc.collect()
                    self._drop_caches()
                    continue

                _t = time.perf_counter()
                with self.step2_profiler.time_stage("postprocess", labels_count=n_raw, **tile_profile_base):
                    cy, cx = self._centroids_vectorised(local_mask)
                stage_seconds["postprocess"] = stage_seconds.get("postprocess", 0.0) + (time.perf_counter() - _t)

                keep_labels = []
                _t = time.perf_counter()
                with self.step2_profiler.time_stage("relabel", labels_count=n_raw, **tile_profile_base):
                    for label_idx in range(n_raw):
                        lcy = cy[label_idx]
                        lcx = cx[label_idx]
                        if (lcy >= local_oy0 and lcy < local_oy1 and
                                lcx >= local_ox0 and lcx < local_ox1):
                            keep_labels.append(label_idx + 1)
                stage_seconds["relabel"] = stage_seconds.get("relabel", 0.0) + (time.perf_counter() - _t)

                if not keep_labels:
                    self.step2_profiler.record_tile_metadata(profile_tile_id, labels_count=0, output_path=self._abs(raw_mask_tile_path))
                    stage_seconds["_profile_tile_id"] = profile_tile_id
                    self._profile_tile_line(i, n_tiles, stage_seconds, 0, tile_shape)
                    self.tile_done.emit(i, n_tiles, 0)
                    del local_mask, cy, cx
                    gc.collect()
                    self._drop_caches()
                    continue

                _t = time.perf_counter()
                with self.step2_profiler.time_stage("relabel", labels_count=len(keep_labels), **tile_profile_base):
                    lut = np.zeros(n_raw + 1, dtype=np.uint32)
                    for new_id, lab in enumerate(keep_labels, start=1):
                        lut[lab] = new_id + global_id_offset

                    remapped = lut[local_mask]
                    remapped_nuclei = None
                    if is_hq and local_nuclei is not None:
                        safe_nuclei = np.where(local_nuclei <= n_raw, local_nuclei, 0).astype(np.uint32, copy=False)
                        remapped_nuclei = lut[safe_nuclei]
                    remapped_hq2_layers = {}
                    if is_hq2 and local_hq2_layers:
                        for layer_key, layer_arr in local_hq2_layers.items():
                            layer_arr = np.asarray(layer_arr, dtype=np.uint32)
                            safe = np.where(layer_arr <= n_raw, layer_arr, 0).astype(np.uint32, copy=False)
                            remapped_hq2_layers[layer_key] = lut[safe]
                    if is_hq and local_qc_rows:
                        kept_set = set(keep_labels)
                        for row_qc in local_qc_rows:
                            old_id = int(row_qc.get("cell_id", 0) or 0)
                            if old_id not in kept_set:
                                continue
                            new_row = dict(row_qc)
                            new_row["cell_id"] = int(lut[old_id])
                            hq_qc_rows.append(new_row)
                    if is_hq2 and local_hq2_metadata:
                        tile_meta = dict(local_hq2_metadata)
                        tile_meta.update({"row": row, "col": col, "out_prefix": ""})
                        self._hq2_tile_metadata.append(tile_meta)
                stage_seconds["relabel"] = stage_seconds.get("relabel", 0.0) + (time.perf_counter() - _t)
                del local_mask, lut, cy, cx

                _t = time.perf_counter()
                with self.step2_profiler.time_stage("merge_or_write", labels_count=len(keep_labels), **tile_profile_base):
                    dst = mmap[ry0:ry1, rx0:rx1]
                    np.copyto(dst, remapped, where=(remapped > 0))
                    del remapped
                    if is_hq and nuclei_mmap is not None and remapped_nuclei is not None:
                        ndst = nuclei_mmap[ry0:ry1, rx0:rx1]
                        np.copyto(ndst, remapped_nuclei, where=(remapped_nuclei > 0))
                        del remapped_nuclei
                    if is_hq2 and remapped_hq2_layers:
                        for layer_key, layer_arr in remapped_hq2_layers.items():
                            layer_mmap = hq2_layer_mmaps.get(layer_key)
                            if layer_mmap is None:
                                continue
                            ldst = layer_mmap[ry0:ry1, rx0:rx1]
                            np.copyto(ldst, layer_arr, where=(layer_arr > 0))
                        del remapped_hq2_layers
                stage_seconds["merge_or_write"] = stage_seconds.get("merge_or_write", 0.0) + (time.perf_counter() - _t)
                self.step2_profiler.log_tile_stage(
                    profile_tile_id,
                    "tile_write",
                    stage_seconds.get("merge_or_write", 0.0),
                    **self._prof_metrics(tile_profile_base),
                )

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
                self.step2_profiler.record_tile_metadata(
                    profile_tile_id,
                    labels_count=n_kept,
                    output_path=self._abs(raw_mask_tile_path or mmap_path),
                )

                self.tile_done.emit(i, n_tiles, n_kept)
                _done_msg = (
                    f"✓ Tile [{i+1}/{n_tiles}]  kept={n_kept} cells  "
                    f"total so far={global_id_offset}"
                )
                self.progress.emit(i + 1, n_tiles, _done_msg)
                log.info(_done_msg)
                stage_seconds["_profile_tile_id"] = profile_tile_id
                self._profile_tile_line(i, n_tiles, stage_seconds, n_kept, tile_shape)
                log.debug(f"  [MEM after write] {self._mem_snapshot()}")
                gc.collect()
                if self.recovery_npy_dir is None and use_gpu:
                    self._empty_torch_cache_if_available()
                self._drop_caches()
                log.debug("  [MEM after drop_caches] " + self._mem_snapshot())

            if prefetcher is not None:
                metrics = prefetcher.snapshot_metrics()
                self.step2_profiler.log_tile_stage(None, "tile_prefetch_wait", metrics.get("prefetch_wait_seconds", 0.0), **metrics)
                prefetcher.close()

            if self.recovery_npy_dir is None:
                del model
                gc.collect()
                self._empty_torch_cache_if_available()
                self._drop_caches()
                log.info(f"All inference done. {self._mem_snapshot()}")

            with self.step2_profiler.time_stage("merge_all_tiles", method=self.method, output_path=self._abs(mmap_path)):
                mmap.flush()
                dapi_mmap.flush()
                if nuclei_mmap is not None:
                    nuclei_mmap.flush()
                for layer_mmap in hq2_layer_mmaps.values():
                    layer_mmap.flush()
            total_cells = int(global_id_offset)
            _out_msg = f"Inference done. Total cells: {total_cells:,}. Writing outputs…"
            self.progress.emit(n_tiles, n_tiles, _out_msg)
            log.info(_out_msg)

            del mmap, dapi_mmap
            if nuclei_mmap is not None:
                del nuclei_mmap
            for layer_key in list(hq2_layer_mmaps.keys()):
                del hq2_layer_mmaps[layer_key]
            gc.collect()
            self._drop_caches()

            mmap_ro      = np.memmap(mmap_path,      dtype='uint32', mode='r',
                                     shape=(full_h, full_w))
            dapi_mmap_ro = np.memmap(dapi_mmap_path, dtype='uint16', mode='r',
                                     shape=(full_h, full_w))
            nuclei_mmap_ro = None
            if is_hq and nuclei_mmap_path:
                nuclei_mmap_ro = np.memmap(nuclei_mmap_path, dtype='uint32', mode='r',
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
            with self.step2_profiler.time_stage("write_mask_zarr", method=self.method, output_path=self._abs(out_zarr_path)):
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
            with self.step2_profiler.time_stage("export_mask_ome_tiff", method=self.method, output_path=self._abs(ome_path)):
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
            with self.step2_profiler.time_stage("export_ome_tiff", method=self.method, output_path=self._abs(global_dapi_path)):
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

            nuclei_ome_path = ""
            nuclei_zarr_path = ""
            qc_table_path = ""
            if is_hq and nuclei_mmap_ro is not None:
                nuclei_zarr_path = os.path.join(self.output_dir, 'global_nuclei_mask.zarr')
                nz = zarr.open(
                    nuclei_zarr_path, mode='w',
                    shape=(full_h, full_w), dtype='uint32',
                    chunks=(1024, 1024),
                )
                with self.step2_profiler.time_stage("write_mask_zarr", method=self.method, output_path=self._abs(nuclei_zarr_path)):
                    for ci, y in enumerate(range(0, full_h, CHUNK_ROWS)):
                        y1 = min(y + CHUNK_ROWS, full_h)
                        nz[y:y1, :] = nuclei_mmap_ro[y:y1, :]
                nuclei_ome_path = os.path.join(self.output_dir, 'global_nuclei_mask.ome.tiff')
                with self.step2_profiler.time_stage("export_mask_ome_tiff", method=self.method, output_path=self._abs(nuclei_ome_path)):
                    with tifffile.TiffWriter(nuclei_ome_path, bigtiff=True) as tif:
                        tif.write(
                            nuclei_mmap_ro.astype(np.float32),
                            tile=(512, 512),
                            compression='lzw',
                            photometric='minisblack',
                            metadata=None,
                        )
                if is_mesmer_guided:
                    qc_table_path = ""
                elif is_hq2:
                    qc_table_path = os.path.join(self.output_dir, 'hq2_qc_table.csv')
                    with self.step2_profiler.time_stage("metadata_write", method=self.method, output_path=self._abs(qc_table_path)):
                        write_hq2_qc_table(qc_table_path, hq_qc_rows)
                else:
                    qc_table_path = os.path.join(self.output_dir, 'hq_qc_table.csv')
                    with self.step2_profiler.time_stage("metadata_write", method=self.method, output_path=self._abs(qc_table_path)):
                        write_hq_qc_table(qc_table_path, hq_qc_rows)
                del nuclei_mmap_ro

            hq2_paths = {}
            if is_hq2:
                hq2_paths = self._write_hq2_layer_outputs(
                    hq2_layer_mmap_paths, (full_h, full_w), out_prefix=""
                )

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
                'tile_strategy':   self._step2_engine_meta(),
                'tile_strategy_mode': self._tile_strategy_info.get("tile_strategy_mode", "manual"),
                'suggested_tile_h': self._tile_strategy_info.get("suggested_tile_h"),
                'suggested_tile_w': self._tile_strategy_info.get("suggested_tile_w"),
                'actual_tile_h':   self._tile_strategy_info.get("actual_tile_h"),
                'actual_tile_w':   self._tile_strategy_info.get("actual_tile_w"),
                'estimated_tile_mpx': self._tile_strategy_info.get("estimated_tile_mpx"),
                'tile_stats':     tile_stats,
                'seg_config':     self.seg_config,
                'cp_params':      self.seg_config,
                'config_path':    self._abs(config_path),
                'created_at':     datetime.now().isoformat(),
            }
            if self.method in (MESMER_WHOLE_CELL, MESMER_NUCLEI, MESMER_NUCLEAR_GUIDED):
                meta.update(mesmer_metadata(
                    self.method,
                    self.seg_config,
                    getattr(model, "get", lambda _k, _d=None: _d)("mesmer_device_status") if isinstance(model, dict) else None,
                    output_mask_path=self._abs(ome_path),
                    extra={
                        "nuclei_mask_path": self._abs(nuclei_ome_path),
                        "whole_cell_mask_path": self._abs(ome_path) if self.method != MESMER_NUCLEI else "",
                    },
                ))
            elif is_hq2:
                hq2_meta_paths = dict(hq2_paths)
                hq2_meta_paths.update({
                    "nuclei_mask_path": self._abs(nuclei_ome_path),
                    "final_cell_mask_path": self._abs(ome_path),
                    "qc_table_path": self._abs(qc_table_path),
                })
                meta.update(hq2_metadata_fields(self.seg_config, hq2_meta_paths))
                meta["hq2_tile_metadata"] = list(self._hq2_tile_metadata)
            elif is_hq:
                meta.update(self._hq_meta_fields(nuclei_ome_path, ome_path, qc_table_path))
            fusion_path = self._fusion_source_path()
            meta["fused_zarr_path"] = fusion_path
            meta["input_zarr"] = fusion_path
            meta["source_zarr"] = fusion_path
            meta["paths"] = {
                "dapi_ome": self._abs(global_dapi_path),
                "mask_ome": self._abs(ome_path),
                "mask_zarr": self._abs(out_zarr_path),
                "fusion_zarr": fusion_path,
                "corrected_channels_zarr": self._multichannel_source_path(),
                "raw_ome": "",
            }
            runtime_meta = self._finish_runtime_monitor()
            meta["runtime"] = runtime_meta
            meta_path = os.path.join(self.output_dir, 'segmentation_meta.json')
            with self.step2_profiler.time_stage("write_segmentation_meta", method=self.method, output_path=self._abs(meta_path)):
                with open(meta_path, 'w') as f:
                    json.dump(meta, f, indent=2)
            self._register_completed_result(meta)
            if self.roi_dir and self.roi_id:
                rel_run_path = os.path.relpath(self.output_dir, self.roi_dir)
                update_roi_segmentation_run(self.roi_dir, {
                    "run_id": self.result_id,
                    "method": self.method,
                    "created_at": self.created_at,
                    "path": rel_run_path,
                    "status": "done",
                    "meta_path": os.path.join(rel_run_path, "segmentation_meta.json"),
                })
                log.info("[Step2] updated roi_index latest_by_method")

            log.info(
                f"=== Segmentation complete ===  "
                f"total_cells={total_cells:,}  "
                f"output={self.output_dir}"
            )
            log.info(f"Final memory: {self._mem_snapshot()}")
            self._record_engine_metrics()
            profile_summary = self.step2_profiler.finalize()
            self._profile_summary(profile_summary)
            if getattr(self, "_channel_store", None) is not None:
                self._channel_store.close()
            self.finished.emit(self.output_dir, total_cells)

        except Exception:
            self._finish_runtime_monitor()
            self._record_engine_metrics()
            profile_summary = self.step2_profiler.finalize()
            self._profile_summary(profile_summary)
            if getattr(self, "_channel_store", None) is not None:
                self._channel_store.close()
            tb = traceback.format_exc()
            if self._logger:
                self._logger.critical("FATAL ERROR:\n" + tb)
                try:
                    self._logger.critical(
                        f"Memory at crash: {self._mem_snapshot()}"
                    )
                except Exception:
                    pass
            self.error.emit(tb)
