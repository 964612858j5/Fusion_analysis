"""Block N4: the runtime monitor's CPU-fallback diagnosis no longer crashes.

`_cpu_fallback_reasons()` used two of `diagnose()`'s local names
(`likely_gpu_inference`, `gpu_peak_util`) in a leftover line and raised
NameError whenever inference looked like a fall back to the CPU (GPU memory
grew, GPU utilisation stayed low, CPU high) -- Step2 then ended with an error
at its very end and the run was not registered.
"""

from block01.utils.runtime_resource_monitor import RuntimeResourceMonitor


def _samples():
    base = {"stage": "model_inference", "gpu_utilization_percent": 2.0, "cpu_percent": 95.0}
    return [dict(base, gpu_memory_used_mb=100.0), dict(base, gpu_memory_used_mb=900.0)]


def test_a_cpu_fallback_is_diagnosed_without_an_error():
    mon = RuntimeResourceMonitor(backend="stardist_nuclei_expansion",
                                 seg_config={"use_gpu": True})
    mon.samples = _samples()
    got = mon.diagnose()
    assert got["likely_cpu_fallback"] is True
    assert got["possible_reasons"]                       # the reasons, not a crash


def test_no_fallback_no_reasons():
    mon = RuntimeResourceMonitor(backend="stardist_nuclei_expansion")
    mon.samples = [{"stage": "model_inference", "gpu_utilization_percent": 80.0,
                    "cpu_percent": 20.0, "gpu_memory_used_mb": 500.0}]
    got = mon.diagnose()
    assert got["likely_cpu_fallback"] is False and got["possible_reasons"] == []


def test_mesmer_device_is_the_engine_process_report_and_tensorflow_is_never_loaded():
    # Mesmer runs on PyTorch in its own process; asking TensorFlow printed
    # CUDA-11 library errors and labelled a GPU run "cpu".
    import os
    import subprocess
    import sys
    import block01
    root = os.path.dirname(os.path.abspath(block01.__file__))     # THIS checkout, whatever its folder name
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('block01', {os.path.join(root, '__init__.py')!r},\n"
        f"                                              submodule_search_locations=[{root!r}])\n"
        "mod = importlib.util.module_from_spec(spec); sys.modules['block01'] = mod; spec.loader.exec_module(mod)\n"
        "from block01.utils.runtime_resource_monitor import RuntimeResourceMonitor as M\n"
        "m = M(backend='mesmer_nuclear_guided', seg_config={'use_gpu': True})\n"
        "got = [m.detect_backend_runtime_device()]\n"
        "for dev in ('gpu', 'cpu'):\n"
        "    m.set_backend_context(backend='mesmer_nuclear_guided', backend_obj={'runner': None, 'device': dev},\n"
        "                          seg_config={'use_gpu': True})\n"
        "    got.append(m.detect_backend_runtime_device())\n"
        "print(got, 'tensorflow' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "['unknown', 'gpu', 'cpu'] False", out.stdout + out.stderr


def _engine_monitor(device, use_gpu=True, backend="mesmer_nuclear_guided"):
    mon = RuntimeResourceMonitor(backend=backend, seg_config={"use_gpu": use_gpu})
    mon.set_backend_context(backend_obj={"runner": object(), "device": device})
    return mon


def test_an_engine_reporting_the_gpu_is_never_called_a_cpu_fallback():
    # samples that look like a fall back (memory grew, GPU idle, CPU busy):
    # the guess must not override what the engine process reported
    for backend, device in (("mesmer_nuclear_guided", "gpu"), ("cellpose_nuclei", "cuda:0")):
        mon = _engine_monitor(device, backend=backend)
        mon.samples = _samples()
        got = mon.diagnose()
        assert got["backend_runtime_device"] == "gpu", backend
        assert got["likely_cpu_fallback"] is False and got["possible_reasons"] == []


def test_an_engine_reporting_the_cpu_stays_cpu_whatever_the_card_does():
    # another program keeping the card busy is not this run on the GPU
    busy = [{"stage": "model_inference", "gpu_utilization_percent": 95.0,
             "cpu_percent": 90.0, "gpu_memory_used_mb": 8000.0}]
    mon = _engine_monitor("cpu", use_gpu=False)
    mon.samples = busy
    got = mon.diagnose()
    assert got["backend_runtime_device"] == "cpu"
    assert got["likely_gpu_inference"] is False and got["likely_cpu_fallback"] is False


def test_an_engine_on_the_cpu_although_use_gpu_is_on_is_a_real_fallback():
    mon = _engine_monitor("cpu", use_gpu=True)
    mon.samples = [{"stage": "model_inference", "gpu_utilization_percent": 0.0,
                    "cpu_percent": 90.0, "gpu_memory_used_mb": 0.0}]
    got = mon.diagnose()
    assert got["likely_cpu_fallback"] is True
    assert got["possible_reasons"] and "engine process reports the CPU" in got["possible_reasons"][0]
    assert mon.console_line(mon.sample("model_inference")).count("device=cpu_fallback") == 1


def test_a_recovery_run_segments_nothing_so_no_device_is_claimed():
    # .npy masks read back under the "model_inference" stage, the card busy
    # with another program: neither GPU inference nor a CPU fallback
    mon = RuntimeResourceMonitor(backend="cellpose_nuclei", seg_config={"use_gpu": True})
    mon.set_backend_context(backend_obj={"recovery": True})
    for samples in (_samples(), [{"stage": "model_inference", "gpu_utilization_percent": 95.0,
                                  "cpu_percent": 90.0, "gpu_memory_used_mb": 8000.0}]):
        mon.samples = samples
        got = mon.diagnose()
        assert got["backend_runtime_device"] == "none"
        assert not (got["likely_gpu_inference"] or got["likely_cpu_fallback"] or got["actual_cuda_execution"])
        assert got["possible_reasons"] == []


def test_cellpose_device_comes_from_its_engine_and_execution_needs_a_task():
    mon = _engine_monitor("cuda:0", backend="cellpose_nuclei")
    mon.samples = [{"stage": "startup", "gpu_utilization_percent": 0.0, "cpu_percent": 5.0}]
    got = mon.diagnose()
    assert got["cellpose_actual_device"] == "cuda:0"
    assert got["actual_cuda_execution"] is False                  # started, nothing run yet
    mon.samples.append({"stage": "model_inference", "gpu_utilization_percent": 60.0, "cpu_percent": 20.0})
    assert mon.diagnose()["actual_cuda_execution"] is True


def test_the_first_sample_already_knows_recovery_and_engine_runs(tmp_path, monkeypatch):
    # the first sample is written when the monitor starts, before the engine
    # (or the recovery read) begins: it must not guess from this process
    from block01.workers import segment_merge_worker as smw
    seen = []
    monkeypatch.setattr(smw.SegmentMergeWorker, "_sample_runtime_resources",
                        lambda self: seen.append(self._runtime_monitor.detect_backend_runtime_device()))
    cases = (("cellpose_nuclei_dapi", str(tmp_path), "none"),     # recovery
             ("cellpose_nuclei_dapi", None, "unknown"),           # engine, not reported yet
             ("mesmer_nuclear_guided", None, "unknown"))
    for method, recovery, want in cases:
        w = smw.SegmentMergeWorker.__new__(smw.SegmentMergeWorker)
        w.method, w.recovery_npy_dir, w.seg_config = method, recovery, {"use_gpu": False}
        w.output_dir, w._logger, w._stop = str(tmp_path), None, True      # _stop: the loop thread ends at once
        w._start_runtime_monitor(interval_s=0)
        w._mem_log_active = False
        assert seen[-1] == want, (method, recovery, seen)
        diag = w._runtime_monitor.diagnose()
        assert not (diag["likely_gpu_inference"] or diag["likely_cpu_fallback"] or diag["actual_cuda_execution"])
