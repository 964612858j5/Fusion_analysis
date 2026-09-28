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
