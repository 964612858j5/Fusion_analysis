"""Best-effort profiling utilities for Step2 segmentation.

All public methods are intentionally fail-soft: profiling must never change
segmentation results or abort a run.
"""

from __future__ import annotations

import csv
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime


class Step2Profiler:
    CSV_FIELDS = [
        "run_id",
        "method",
        "tile_id",
        "stage",
        "seconds",
        "rss_mb",
        "rss_delta_mb",
        "peak_rss_mb",
        "gpu_mem_mb",
        "gpu_peak_mb",
        "tile_h",
        "tile_w",
        "labels_count",
        "timestamp",
        "bbox_global",
        "bbox_local",
        "tile_shape",
        "overlap",
        "channels_used",
        "dtype",
        "input_source",
        "output_path",
        "prefetch_hit",
        "prefetch_miss",
        "prefetch_queue_depth",
        "cache_hit",
        "cache_miss",
        "cache_bytes",
        "cache_evictions",
    ]

    def __init__(self, enabled=True, output_dir=None, run_id=None, method=None):
        self.enabled = bool(enabled)
        if not self.enabled:
            return
        try:
            self.output_dir = os.path.abspath(output_dir or os.getcwd())
            self.run_id = str(run_id or uuid.uuid4())
            self.method = str(method or "")
            self.events = []
            self.tile_metadata = {}
            self._active = {}
            self._stage_totals = {}
            self._tile_stage_totals = {}
            self._started_at = time.perf_counter()
            self._created_at = datetime.now().isoformat()
            self._peak_rss_mb = None
            self._peak_gpu_mb = None
            os.makedirs(self.output_dir, exist_ok=True)
        except Exception:
            self.enabled = False

    def start_stage(self, name, **context):
        if not getattr(self, "enabled", False):
            return None
        try:
            key = self._stage_key(name, context)
            self._active[key] = {
                "name": str(name),
                "context": dict(context or {}),
                "start": time.perf_counter(),
                "memory": self.snapshot_memory(),
                "gpu": self.snapshot_gpu(),
            }
            return key
        except Exception:
            return None

    def end_stage(self, name, **context):
        if not getattr(self, "enabled", False):
            return None
        try:
            key = context.pop("_profile_key", None) or self._stage_key(name, context)
            active = self._active.pop(key, None)
            end_memory = self.snapshot_memory()
            end_gpu = self.snapshot_gpu()
            seconds = None
            start_memory = None
            start_gpu = None
            merged_context = dict(context or {})
            if active:
                seconds = max(0.0, time.perf_counter() - float(active.get("start", 0.0)))
                start_memory = active.get("memory")
                start_gpu = active.get("gpu")
                merged_context.update(active.get("context") or {})
                merged_context.update(context or {})
            else:
                seconds = float(merged_context.pop("seconds", 0.0) or 0.0)
            event = self._build_event(
                str(name),
                seconds,
                merged_context,
                start_memory=start_memory,
                end_memory=end_memory,
                start_gpu=start_gpu,
                end_gpu=end_gpu,
            )
            self._record_event(event)
            return event
        except Exception:
            return None

    @contextmanager
    def time_stage(self, name, **context):
        if not getattr(self, "enabled", False):
            yield None
            return
        key = self.start_stage(name, **context)
        try:
            yield key
        finally:
            try:
                self.end_stage(name, _profile_key=key, **context)
            except Exception:
                pass

    def log_tile_stage(self, tile_id, stage, seconds, **metrics):
        if not getattr(self, "enabled", False):
            return None
        try:
            metrics = dict(metrics or {})
            metrics["tile_id"] = tile_id
            event = self._build_event(str(stage), float(seconds or 0.0), metrics)
            self._record_event(event)
            return event
        except Exception:
            return None

    def record_tile_metadata(self, tile_key, **metadata):
        if not getattr(self, "enabled", False):
            return
        try:
            current = self.tile_metadata.setdefault(str(tile_key), {})
            current.update(self._jsonable(metadata or {}))
        except Exception:
            pass

    def snapshot_memory(self):
        if not getattr(self, "enabled", False):
            return None
        try:
            import psutil

            rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)
            if self._peak_rss_mb is None or rss_mb > self._peak_rss_mb:
                self._peak_rss_mb = rss_mb
            return {"rss_mb": rss_mb, "peak_rss_mb": self._peak_rss_mb}
        except Exception:
            return None

    def snapshot_gpu(self):
        if not getattr(self, "enabled", False):
            return None
        best = None
        try:
            import torch

            if torch.cuda.is_available():
                mem = 0.0
                peak = 0.0
                for idx in range(torch.cuda.device_count()):
                    mem += torch.cuda.memory_reserved(idx) / (1024.0 * 1024.0)
                    peak += torch.cuda.max_memory_reserved(idx) / (1024.0 * 1024.0)
                best = {"gpu_mem_mb": mem, "gpu_peak_mb": peak, "source": "torch"}
        except Exception:
            pass
        if best is None:
            try:
                import cupy

                mem = 0.0
                peak = 0.0
                for idx in range(cupy.cuda.runtime.getDeviceCount()):
                    with cupy.cuda.Device(idx):
                        pool = cupy.get_default_memory_pool()
                        mem += pool.used_bytes() / (1024.0 * 1024.0)
                        peak = max(peak, mem)
                best = {"gpu_mem_mb": mem, "gpu_peak_mb": peak, "source": "cupy"}
            except Exception:
                pass
        if best is None:
            try:
                import tensorflow as tf

                infos = []
                for gpu in tf.config.list_physical_devices("GPU"):
                    try:
                        infos.append(tf.config.experimental.get_memory_info(gpu.name))
                    except Exception:
                        pass
                if infos:
                    mem = sum(float(info.get("current", 0)) for info in infos) / (1024.0 * 1024.0)
                    peak = sum(float(info.get("peak", 0)) for info in infos) / (1024.0 * 1024.0)
                    best = {"gpu_mem_mb": mem, "gpu_peak_mb": peak, "source": "tensorflow"}
            except Exception:
                pass
        try:
            if best is not None:
                peak_value = best.get("gpu_peak_mb") or best.get("gpu_mem_mb")
                if peak_value is not None and (self._peak_gpu_mb is None or peak_value > self._peak_gpu_mb):
                    self._peak_gpu_mb = peak_value
                best["gpu_peak_mb"] = self._peak_gpu_mb
        except Exception:
            pass
        return best

    def finalize(self):
        if not getattr(self, "enabled", False):
            return {}
        try:
            total_runtime = max(0.0, time.perf_counter() - self._started_at)
            self.log_tile_stage(None, "total_runtime", total_runtime)
            summary = self._summary(total_runtime)
            payload = {
                "version": 1,
                "run_id": self.run_id,
                "method": self.method,
                "created_at": self._created_at,
                "finalized_at": datetime.now().isoformat(),
                "output_dir": self.output_dir,
                "summary": summary,
                "events": self.events,
                "tile_metadata": self.tile_metadata,
            }
            json_path = os.path.join(self.output_dir, "step2_profile.json")
            csv_path = os.path.join(self.output_dir, "step2_profile.csv")
            summary_path = os.path.join(self.output_dir, "step2_profile_summary.txt")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.CSV_FIELDS)
                writer.writeheader()
                for event in self.events:
                    writer.writerow({key: self._csv_value(event.get(key)) for key in self.CSV_FIELDS})
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(self.format_summary(summary))
            summary["json_path"] = json_path
            summary["csv_path"] = csv_path
            summary["summary_path"] = summary_path
            return summary
        except Exception:
            return {}

    def format_summary(self, summary):
        try:
            lines = ["Summary:"]
            total = float(summary.get("total_runtime", 0.0) or 0.0)
            lines.append(f"total={total:.1f}s")
            for stage, seconds in summary.get("per_stage_seconds", {}).items():
                if stage == "total_runtime":
                    continue
                pct = (seconds / total * 100.0) if total > 0 else 0.0
                lines.append(f"{stage}={seconds:.1f}s ({pct:.1f}%)")
            if summary.get("slowest_tile") is not None:
                lines.append(f"slowest_tile={summary.get('slowest_tile')} ({summary.get('slowest_tile_seconds', 0.0):.1f}s)")
            if summary.get("slowest_stage"):
                lines.append(f"slowest_stage={summary.get('slowest_stage')} ({summary.get('slowest_stage_seconds', 0.0):.1f}s)")
            lines.append(f"average_tile_time={summary.get('average_tile_time', 0.0):.1f}s")
            lines.append(f"max_memory={summary.get('max_rss_mb', 0.0):.0f}MB")
            if summary.get("max_gpu_mem_mb") is not None:
                lines.append(f"max_gpu_memory={summary.get('max_gpu_mem_mb', 0.0):.0f}MB")
            lines.append(f"cache_hit_rate={summary.get('cache_hit_rate', 0.0):.1f}%")
            lines.append(f"io_hidden_by_prefetch={summary.get('io_hidden_by_prefetch_seconds', 0.0):.1f}s")
            lines.append(f"gpu_idle_estimate={summary.get('gpu_idle_estimate_seconds', 0.0):.1f}s")
            lines.append(f"suspected_bottleneck={summary.get('suspected_bottleneck') or 'unknown'}")
            return "\n".join(lines) + "\n"
        except Exception:
            return "Summary:\nsuspected_bottleneck=unknown\n"

    def _summary(self, total_runtime):
        stage_totals = dict(sorted(self._stage_totals.items(), key=lambda kv: kv[1], reverse=True))
        tile_totals = {}
        for tile_id, stages in self._tile_stage_totals.items():
            tile_totals[tile_id] = sum(float(v or 0.0) for stage, v in stages.items() if stage != "total")
        slowest_tile = None
        slowest_tile_seconds = 0.0
        if tile_totals:
            slowest_tile, slowest_tile_seconds = max(tile_totals.items(), key=lambda kv: kv[1])
        slowest_stage = None
        slowest_stage_seconds = 0.0
        stage_without_total = {k: v for k, v in stage_totals.items() if k != "total_runtime"}
        if stage_without_total:
            slowest_stage, slowest_stage_seconds = max(stage_without_total.items(), key=lambda kv: kv[1])
        max_rss = max([e.get("rss_mb") or 0.0 for e in self.events] + [self._peak_rss_mb or 0.0])
        max_gpu = max([e.get("gpu_mem_mb") or 0.0 for e in self.events] + [self._peak_gpu_mb or 0.0])
        cache_hits = sum(float(e.get("cache_hit") or 0.0) for e in self.events)
        cache_misses = sum(float(e.get("cache_miss") or 0.0) for e in self.events)
        prefetch_hits = sum(float(e.get("prefetch_hit") or 0.0) for e in self.events)
        prefetch_misses = sum(float(e.get("prefetch_miss") or 0.0) for e in self.events)
        prefetch_wait = stage_totals.get("tile_prefetch_wait", 0.0)
        read_tile = stage_totals.get("read_tile", 0.0)
        inference = stage_totals.get("model_inference", 0.0)
        cache_total = cache_hits + cache_misses
        prefetch_total = prefetch_hits + prefetch_misses
        return {
            "total_runtime": float(total_runtime or 0.0),
            "per_stage_seconds": stage_totals,
            "slowest_tile": slowest_tile,
            "slowest_tile_seconds": float(slowest_tile_seconds or 0.0),
            "slowest_stage": slowest_stage,
            "slowest_stage_seconds": float(slowest_stage_seconds or 0.0),
            "average_tile_time": (sum(tile_totals.values()) / len(tile_totals)) if tile_totals else 0.0,
            "max_rss_mb": float(max_rss or 0.0),
            "max_gpu_mem_mb": float(max_gpu) if max_gpu else None,
            "suspected_bottleneck": slowest_stage,
            "cache_hit_rate": (cache_hits / cache_total * 100.0) if cache_total else 0.0,
            "prefetch_hit_rate": (prefetch_hits / prefetch_total * 100.0) if prefetch_total else 0.0,
            "io_hidden_by_prefetch_seconds": max(0.0, read_tile - prefetch_wait),
            "gpu_idle_estimate_seconds": float(prefetch_wait or 0.0),
        }

    def _record_event(self, event):
        self.events.append(event)
        stage = event.get("stage") or ""
        seconds = float(event.get("seconds") or 0.0)
        self._stage_totals[stage] = self._stage_totals.get(stage, 0.0) + seconds
        tile_id = event.get("tile_id")
        if tile_id not in (None, ""):
            tile_key = str(tile_id)
            stages = self._tile_stage_totals.setdefault(tile_key, {})
            stages[stage] = stages.get(stage, 0.0) + seconds

    def _build_event(self, stage, seconds, context, start_memory=None, end_memory=None, start_gpu=None, end_gpu=None):
        context = dict(context or {})
        mem = end_memory if end_memory is not None else self.snapshot_memory()
        gpu = end_gpu if end_gpu is not None else self.snapshot_gpu()
        rss_mb = (mem or {}).get("rss_mb")
        start_rss = (start_memory or {}).get("rss_mb")
        gpu_mem_mb = (gpu or {}).get("gpu_mem_mb")
        event = {
            "run_id": self.run_id,
            "method": context.pop("method", None) or self.method,
            "tile_id": context.pop("tile_id", None),
            "stage": stage,
            "seconds": float(seconds or 0.0),
            "rss_mb": rss_mb,
            "rss_delta_mb": (rss_mb - start_rss) if rss_mb is not None and start_rss is not None else None,
            "peak_rss_mb": (mem or {}).get("peak_rss_mb", self._peak_rss_mb),
            "gpu_mem_mb": gpu_mem_mb,
            "gpu_peak_mb": (gpu or {}).get("gpu_peak_mb", self._peak_gpu_mb),
            "timestamp": datetime.now().isoformat(),
        }
        event.update(self._jsonable(context))
        return event

    @staticmethod
    def _stage_key(name, context):
        tile_id = (context or {}).get("tile_id")
        return f"{name}:{tile_id}:{len(str(context or {}))}"

    @staticmethod
    def _jsonable(value):
        if isinstance(value, dict):
            return {str(k): Step2Profiler._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [Step2Profiler._jsonable(v) for v in value]
        try:
            json.dumps(value)
            return value
        except Exception:
            return str(value)

    @staticmethod
    def _csv_value(value):
        if isinstance(value, (list, tuple, dict)):
            return json.dumps(value, ensure_ascii=False)
        return value
