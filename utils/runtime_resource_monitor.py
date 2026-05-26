"""Runtime CPU/GPU monitoring and Step2 device diagnosis.

This module is intentionally fail-soft. Missing optional dependencies or GPU
driver errors should disable only monitoring, never segmentation.
"""

from __future__ import annotations

import os
import time
from datetime import datetime


class RuntimeResourceMonitor:
    """Best-effort CPU/GPU sampler with lightweight inference heuristics."""

    def __init__(self, backend="", logger=None, backend_obj=None, seg_config=None):
        self.backend = str(backend or "")
        self.logger = logger
        self.backend_obj = backend_obj
        self.seg_config = dict(seg_config or {})
        self.current_stage = "startup"
        self.samples = []
        self.warnings = []
        self._last_console_at = 0.0
        self._last_gpu_mem_mb = None
        self._nvml = None
        self._nvml_handle = None
        self._nvml_enabled = False
        self._torch_gpu_enabled = False
        self._init_gpu_monitoring()

    def set_backend_context(self, backend=None, backend_obj=None, seg_config=None):
        if backend is not None:
            self.backend = str(backend or "")
        if backend_obj is not None:
            self.backend_obj = backend_obj
        if seg_config is not None:
            self.seg_config = dict(seg_config or {})

    def set_stage(self, stage):
        self.current_stage = str(stage or "unknown")

    def sample(self, stage=None):
        stage = str(stage or self.current_stage or "unknown")
        cpu_percent, ram_used_gb, ram_total_gb = self._sample_cpu()
        gpu = self._sample_gpu()
        backend_runtime_device = self.detect_backend_runtime_device()
        sample = {
            "timestamp": datetime.now().isoformat(),
            "backend": self.backend,
            "stage": stage,
            "cpu_percent": cpu_percent,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "gpu_available": bool(gpu.get("gpu_available")),
            "gpu_name": gpu.get("gpu_name") or "",
            "gpu_utilization_percent": gpu.get("gpu_utilization_percent"),
            "gpu_memory_used_mb": gpu.get("gpu_memory_used_mb"),
            "gpu_memory_total_mb": gpu.get("gpu_memory_total_mb"),
            "gpu_temperature": gpu.get("gpu_temperature"),
            "backend_runtime_device": backend_runtime_device,
        }
        self.samples.append(sample)
        diagnosis = self.diagnose()
        sample.update({
            "likely_gpu_inference": bool(diagnosis.get("likely_gpu_inference")),
            "likely_cpu_fallback": bool(diagnosis.get("likely_cpu_fallback")),
            "likely_io_bottleneck": bool(diagnosis.get("likely_io_bottleneck")),
            "likely_merge_bottleneck": bool(diagnosis.get("likely_merge_bottleneck")),
        })
        return sample

    def diagnose(self):
        samples = list(self.samples or [])
        inference = [s for s in samples if s.get("stage") == "model_inference"]
        read_or_pre = [s for s in samples if s.get("stage") in {"read_tile", "preprocess", "tile_prepare"}]
        merge_or_write = [s for s in samples if s.get("stage") in {"merge_or_write", "merge_all_tiles", "write_mask_zarr", "export_mask_ome_tiff", "export_ome_tiff"}]
        all_samples = samples or [{}]

        gpu_peak_util = max([float(s.get("gpu_utilization_percent") or 0.0) for s in all_samples] + [0.0])
        gpu_peak_mem = max([float(s.get("gpu_memory_used_mb") or 0.0) for s in all_samples] + [0.0])
        cpu_peak = max([float(s.get("cpu_percent") or 0.0) for s in all_samples] + [0.0])
        inference_gpu_hits = sum(1 for s in inference if float(s.get("gpu_utilization_percent") or 0.0) > 30.0)
        inference_gpu_low = inference and max(float(s.get("gpu_utilization_percent") or 0.0) for s in inference) < 10.0
        inference_cpu_high = inference and max(float(s.get("cpu_percent") or 0.0) for s in inference) >= 70.0

        mem_values = [float(s.get("gpu_memory_used_mb") or 0.0) for s in all_samples]
        gpu_mem_growth = (max(mem_values) - min(mem_values)) if mem_values else 0.0
        likely_gpu_inference = bool(inference_gpu_hits)
        likely_cpu_fallback = bool(inference and gpu_mem_growth >= 128.0 and inference_gpu_low and inference_cpu_high)
        likely_io_bottleneck = bool(read_or_pre and self._stage_hot(read_or_pre) and gpu_peak_util < 20.0)
        likely_merge_bottleneck = bool(merge_or_write and self._stage_hot(merge_or_write) and gpu_peak_util < 20.0)

        device = self.detect_backend_runtime_device()
        if likely_cpu_fallback and device == "gpu":
            device = "cpu_fallback"
        elif likely_gpu_inference:
            device = "gpu"

        cellpose_requested_gpu = str(self.seg_config.get("use_gpu", True)).strip().lower() not in {"0", "false", "no", "off", "cpu"}
        cellpose_actual_device = self._cellpose_model_device() if "cellpose" in self.backend.lower() else "unknown"
        actual_cuda_execution = bool(likely_gpu_inference or ("cuda" in str(cellpose_actual_device).lower() and gpu_peak_util > 0.0))
        gpu_morph_available = None
        cupy_smoke_test = "unknown"
        cucim_smoke_test = "unknown"
        try:
            from ..core import bg_correction
            gpu_morph_available = bool(getattr(bg_correction, "GPU_MORPH_AVAILABLE", False))
            smoke = getattr(bg_correction, "GPU_MORPH_SMOKE_TEST", {}) or {}
            cupy_smoke_test = smoke.get("cupy", "unknown")
            cucim_smoke_test = smoke.get("cucim", "unknown")
        except Exception:
            pass
        reasons = []
        if likely_cpu_fallback:
            reasons.extend(self._cpu_fallback_reasons())

        return {
            "backend_runtime_device": device,
            "likely_gpu_inference": likely_gpu_inference,
            "likely_cpu_fallback": likely_cpu_fallback,
            "actual_cuda_execution": actual_cuda_execution,
            "cellpose_requested_gpu": cellpose_requested_gpu,
            "cellpose_actual_device": cellpose_actual_device,
            "gpu_morphology_available": gpu_morph_available,
            "cupy_smoke_test": cupy_smoke_test,
            "cucim_smoke_test": cucim_smoke_test,
            "likely_io_bottleneck": likely_io_bottleneck,
            "likely_merge_bottleneck": likely_merge_bottleneck,
            "gpu_peak_memory_mb": gpu_peak_mem,
            "gpu_peak_utilization": gpu_peak_util,
            "cpu_peak": cpu_peak,
            "possible_reasons": reasons,
        }

    def console_line(self, sample):
        device = sample.get("backend_runtime_device") or "unknown"
        if sample.get("likely_cpu_fallback"):
            device = "cpu_fallback"
        gpu_mem = self._format_gpu_mem(sample.get("gpu_memory_used_mb"))
        ram = self._format_gb(sample.get("ram_used_gb"))
        return (
            "[RuntimeMonitor]\n"
            f"backend={sample.get('backend') or self.backend}\n"
            f"device={device}\n"
            f"gpu_util={self._fmt_percent(sample.get('gpu_utilization_percent'))}\n"
            f"gpu_mem={gpu_mem}\n"
            f"cpu={self._fmt_percent(sample.get('cpu_percent'))}\n"
            f"ram={ram}"
        )

    def should_print(self, min_interval_s=2.0):
        now = time.time()
        if now - self._last_console_at >= float(min_interval_s or 2.0):
            self._last_console_at = now
            return True
        return False

    def detect_backend_runtime_device(self):
        backend = self.backend.lower()
        if "cellpose" in backend:
            return self._detect_cellpose_device()
        if "mesmer" in backend:
            return self._detect_mesmer_device()
        if "hq" in backend:
            return self._detect_hq_device()
        return "unknown"

    def _init_gpu_monitoring(self):
        try:
            import pynvml

            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            if count > 0:
                self._nvml = pynvml
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self._nvml_enabled = True
                return
        except Exception as exc:
            self._warn(f"pynvml unavailable: {exc}")

        try:
            import torch

            self._torch_gpu_enabled = bool(torch.cuda.is_available())
            if not self._torch_gpu_enabled:
                self._warn("GPU monitoring disabled")
        except Exception as exc:
            self._warn(f"torch CUDA monitor unavailable: {exc}")
            self._warn("GPU monitoring disabled")

    def _sample_cpu(self):
        try:
            import psutil

            cpu_percent = float(psutil.cpu_percent(interval=None))
            mem = psutil.virtual_memory()
            return cpu_percent, float(mem.used) / (1024.0 ** 3), float(mem.total) / (1024.0 ** 3)
        except Exception as exc:
            self._warn(f"CPU monitoring unavailable: {exc}")
            return None, None, None

    def _sample_gpu(self):
        if self._nvml_enabled:
            try:
                nvml = self._nvml
                handle = self._nvml_handle
                util = nvml.nvmlDeviceGetUtilizationRates(handle)
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                name = nvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                temp = None
                try:
                    temp = float(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))
                except Exception:
                    pass
                return {
                    "gpu_available": True,
                    "gpu_name": str(name or ""),
                    "gpu_utilization_percent": float(getattr(util, "gpu", 0.0)),
                    "gpu_memory_used_mb": float(mem.used) / (1024.0 ** 2),
                    "gpu_memory_total_mb": float(mem.total) / (1024.0 ** 2),
                    "gpu_temperature": temp,
                }
            except Exception as exc:
                self._warn(f"pynvml sampling failed: {exc}")
                self._nvml_enabled = False

        if self._torch_gpu_enabled:
            try:
                import torch

                idx = torch.cuda.current_device()
                props = torch.cuda.get_device_properties(idx)
                used = float(torch.cuda.memory_reserved(idx)) / (1024.0 ** 2)
                total = float(getattr(props, "total_memory", 0.0)) / (1024.0 ** 2)
                return {
                    "gpu_available": True,
                    "gpu_name": str(getattr(props, "name", "")),
                    "gpu_utilization_percent": None,
                    "gpu_memory_used_mb": used,
                    "gpu_memory_total_mb": total,
                    "gpu_temperature": None,
                }
            except Exception as exc:
                self._warn(f"torch CUDA sampling failed: {exc}")
        return {
            "gpu_available": False,
            "gpu_name": "",
            "gpu_utilization_percent": None,
            "gpu_memory_used_mb": 0.0,
            "gpu_memory_total_mb": 0.0,
            "gpu_temperature": None,
        }

    def _detect_cellpose_device(self):
        torch_cuda = False
        use_gpu_result = None
        model_device = ""
        try:
            import torch

            torch_cuda = bool(torch.cuda.is_available())
        except Exception:
            pass
        try:
            from cellpose import core as cellpose_core

            if hasattr(cellpose_core, "use_gpu"):
                use_gpu_result = bool(cellpose_core.use_gpu())
        except Exception:
            pass
        try:
            model = (self.backend_obj or {}).get("cellpose") if isinstance(self.backend_obj, dict) else self.backend_obj
            model_device = str(getattr(model, "device", "") or "")
        except Exception:
            model_device = ""
        combined = " ".join([model_device.lower(), str(use_gpu_result).lower()])
        if "cuda" in combined or "gpu" in combined or use_gpu_result is True:
            return "gpu" if torch_cuda else "mixed"
        if torch_cuda and model_device:
            return "mixed"
        if torch_cuda:
            return "unknown"
        return "cpu"

    def _cellpose_model_device(self):
        try:
            model = (self.backend_obj or {}).get("cellpose") if isinstance(self.backend_obj, dict) else self.backend_obj
            return str(getattr(model, "device", "unknown") or "unknown")
        except Exception:
            return "unknown"

    def _detect_mesmer_device(self):
        try:
            status = None
            if isinstance(self.backend_obj, dict):
                status = self.backend_obj.get("mesmer_device_status")
            device_used = str(getattr(status, "device_used", "") or self.seg_config.get("device_used") or "").lower()
            if "gpu" in device_used:
                return "gpu"
            if "cpu" in device_used:
                return "cpu"
        except Exception:
            pass
        try:
            import tensorflow as tf

            return "gpu" if tf.config.list_physical_devices("GPU") else "cpu"
        except Exception:
            return "unknown"

    def _detect_hq_device(self):
        try:
            import cupy  # noqa: F401

            cupy_available = True
        except Exception:
            cupy_available = False
        enabled = bool(self.seg_config.get("gpu_ops_enabled") or self.seg_config.get("use_cupy"))
        if cupy_available and enabled:
            return "mixed"
        return "cpu"

    def _stage_hot(self, samples):
        if not samples:
            return False
        return max(float(s.get("cpu_percent") or 0.0) for s in samples) >= 70.0

    def _cpu_fallback_reasons(self):
        cellpose_requested_gpu = str(self.seg_config.get("use_gpu", True)).strip().lower() not in {"0", "false", "no", "off", "cpu"}
        cellpose_actual_device = self._cellpose_model_device() if "cellpose" in self.backend.lower() else "unknown"
        actual_cuda_execution = bool(likely_gpu_inference or ("cuda" in str(cellpose_actual_device).lower() and gpu_peak_util > 0.0))
        gpu_morph_available = None
        cupy_smoke_test = "unknown"
        cucim_smoke_test = "unknown"
        try:
            from ..core import bg_correction
            gpu_morph_available = bool(getattr(bg_correction, "GPU_MORPH_AVAILABLE", False))
            smoke = getattr(bg_correction, "GPU_MORPH_SMOKE_TEST", {}) or {}
            cupy_smoke_test = smoke.get("cupy", "unknown")
            cucim_smoke_test = smoke.get("cucim", "unknown")
        except Exception:
            pass
        reasons = []
        try:
            import torch

            if not torch.cuda.is_available():
                reasons.append("torch.cuda unavailable")
        except Exception:
            reasons.append("torch unavailable")
        if self.detect_backend_runtime_device() in {"cpu", "mixed"}:
            reasons.append("backend reports non-CUDA device")
        if not reasons:
            reasons.append("GPU memory allocated but inference utilization stayed low")
        return reasons

    def _warn(self, message):
        text = f"[RuntimeMonitor] {message}"
        if text in self.warnings:
            return
        self.warnings.append(text)
        try:
            print(text)
        except Exception:
            pass
        try:
            if self.logger:
                self.logger.warning(text)
        except Exception:
            pass

    @staticmethod
    def _fmt_percent(value):
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.0f}%"
        except Exception:
            return "n/a"

    @staticmethod
    def _format_gb(value):
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.1f}GB"
        except Exception:
            return "n/a"

    @staticmethod
    def _format_gpu_mem(value):
        if value is None:
            return "n/a"
        try:
            mb = float(value or 0.0)
            if mb >= 1024.0:
                return f"{mb / 1024.0:.1f}GB"
            return f"{mb:.0f}MB"
        except Exception:
            return "n/a"
