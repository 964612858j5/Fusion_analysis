#!/usr/bin/env python
"""Diagnose CUDA/PyTorch/CuPy/cuCIM/Cellpose GPU compatibility.

This script is read-only: it does not install, uninstall, or modify drivers,
packages, or CUDA libraries.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
REPO_PARENT = ROOT.parent
for path in (str(REPO_PARENT), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


RESULTS = {}


def line(key, value):
    print(f"{key}: {value}")


def section(title):
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def run_cmd(cmd, timeout=10):
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 999, "", str(exc)


def first_error_text():
    for value in RESULTS.values():
        text = str(value)
        if "cudaErrorCompatNotSupportedOnDevice" in text or "forward compatibility was attempted on non supported HW" in text:
            return text
    for value in RESULTS.values():
        text = str(value)
        if "CUDA" in text or "cuda" in text:
            return text
    return ""


def diagnose_likely_reason():
    text = first_error_text()
    torch_cuda = RESULTS.get("torch.version.cuda")
    torch_ok = RESULTS.get("torch.cuda.is_available")
    cupy_runtime = RESULTS.get("cupy.runtime_version")
    cupy_driver = RESULTS.get("cupy.driver_version")
    driver = RESULTS.get("nvidia.driver_version")
    gpu_name = str(RESULTS.get("nvidia.gpu_name") or "")

    if "forward compatibility was attempted on non supported HW" in text or "cudaErrorCompatNotSupportedOnDevice" in text:
        return (
            "CUDA runtime/driver/GPU compatibility failure. A package CUDA runtime is trying "
            "forward-compat mode that this GPU/driver combination does not support."
        )
    if torch_ok is False and torch_cuda:
        return "PyTorch CUDA build is installed but torch.cuda initialization failed."
    if cupy_runtime and cupy_driver and str(cupy_runtime)[:2] != str(cupy_driver)[:2]:
        return "CuPy CUDA runtime major version appears different from the CUDA driver API version."
    if driver and any(name in gpu_name.upper() for name in ("P100", "V100", "T4", "RTX", "A", "L")):
        return "GPU exists but one or more CUDA backend smoke tests failed; inspect package CUDA builds."
    return "No single cause proven by smoke tests; inspect failed backend sections above."


def recommend_fix():
    text = first_error_text()
    torch_cuda = str(RESULTS.get("torch.version.cuda") or "")
    cupy_version = str(RESULTS.get("cupy.version") or "")
    conda_prefix = RESULTS.get("env.CONDA_PREFIX") or ""

    if "forward compatibility was attempted on non supported HW" in text or "cudaErrorCompatNotSupportedOnDevice" in text:
        return (
            "Use a CUDA runtime build compatible with the installed NVIDIA driver and GPU. "
            "Typical fixes: update the NVIDIA driver, or install PyTorch/CuPy builds for the "
            "driver-supported CUDA line. Avoid mixing pip CUDA wheels and conda cudatoolkit/"
            "pytorch-cuda runtimes in the same env."
        )
    if "12" in torch_cuda and "cupy-cuda11" in cupy_version:
        return "Install matching CuPy CUDA 12.x build or align PyTorch to CUDA 11.x."
    if conda_prefix:
        return (
            "Create a clean env with a single CUDA runtime source, then install matching "
            "pytorch-cuda and cupy-cudaXX packages for the driver. Do not change drivers "
            "from this script."
        )
    return "Run this script inside the target conda env, then align driver, PyTorch CUDA, CuPy CUDA, and cuCIM builds."


def diagnose_nvidia():
    section("NVIDIA / System")
    line("python", sys.executable)
    line("python_version", sys.version.split()[0])
    line("platform", platform.platform())
    for key in ("CONDA_PREFIX", "CUDA_HOME", "CUDA_PATH", "LD_LIBRARY_PATH"):
        RESULTS[f"env.{key}"] = os.environ.get(key, "")
        line(key, RESULTS[f"env.{key}"])

    code, out, err = run_cmd(["nvidia-smi"])
    RESULTS["nvidia-smi.exit_code"] = code
    print("\n[nvidia-smi]")
    print(out or err or "<no output>")

    code, out, err = run_cmd([
        "nvidia-smi",
        "--query-gpu=name,driver_version,cuda_version,memory.total",
        "--format=csv,noheader",
    ])
    RESULTS["nvidia.query"] = out or err
    if out:
        parts = [p.strip() for p in out.splitlines()[0].split(",")]
        if len(parts) >= 4:
            RESULTS["nvidia.gpu_name"] = parts[0]
            RESULTS["nvidia.driver_version"] = parts[1]
            RESULTS["nvidia.cuda_version"] = parts[2]
            RESULTS["nvidia.memory_total"] = parts[3]
    line("NVIDIA driver version", RESULTS.get("nvidia.driver_version", "unknown"))
    line("GPU name", RESULTS.get("nvidia.gpu_name", "unknown"))
    line("nvidia-smi CUDA version", RESULTS.get("nvidia.cuda_version", "unknown"))
    line("GPU memory total", RESULTS.get("nvidia.memory_total", "unknown"))

    code, out, err = run_cmd(["nvcc", "--version"])
    RESULTS["system.nvcc"] = out or err
    line("system nvcc", (out or err or "not found").splitlines()[-1] if (out or err) else "not found")


def diagnose_torch():
    section("PyTorch")
    try:
        import torch

        RESULTS["torch.version"] = torch.__version__
        RESULTS["torch.version.cuda"] = getattr(torch.version, "cuda", None)
        line("torch version", RESULTS["torch.version"])
        line("torch.version.cuda", RESULTS["torch.version.cuda"])
        try:
            available = bool(torch.cuda.is_available())
            RESULTS["torch.cuda.is_available"] = available
            line("torch.cuda.is_available()", available)
            line("torch CUDA driver version", torch._C._cuda_getDriverVersion() if hasattr(torch._C, "_cuda_getDriverVersion") else "n/a")
            line("torch CUDA runtime version", torch._C._cuda_getCompiledVersion() if hasattr(torch._C, "_cuda_getCompiledVersion") else "n/a")
            if available:
                line("torch.cuda.device_count()", torch.cuda.device_count())
                line("torch.cuda.get_device_name(0)", torch.cuda.get_device_name(0))
                x = torch.ones((16, 16), device="cuda")
                y = (x * 2).sum()
                torch.cuda.synchronize()
                RESULTS["torch.cuda_smoke"] = f"pass: {float(y.item())}"
            else:
                RESULTS["torch.cuda_smoke"] = "skipped: cuda unavailable"
            line("torch CUDA smoke", RESULTS["torch.cuda_smoke"])
        except Exception as exc:
            RESULTS["torch.cuda.is_available"] = False
            RESULTS["torch.cuda_error"] = traceback.format_exc()
            line("torch CUDA check error", repr(exc))
    except Exception as exc:
        RESULTS["torch.import_error"] = traceback.format_exc()
        line("torch import", f"failed: {exc}")


def diagnose_cupy():
    section("CuPy / cupyx.scipy.ndimage")
    try:
        import cupy as cp
        import cupyx.scipy.ndimage as ndi

        RESULTS["cupy.version"] = cp.__version__
        line("cupy version", cp.__version__)
        try:
            RESULTS["cupy.runtime_version"] = cp.cuda.runtime.runtimeGetVersion()
            RESULTS["cupy.driver_version"] = cp.cuda.runtime.driverGetVersion()
            line("cupy.cuda.runtime.runtimeGetVersion()", RESULTS["cupy.runtime_version"])
            line("cupy.cuda.runtime.driverGetVersion()", RESULTS["cupy.driver_version"])
        except Exception as exc:
            RESULTS["cupy.version_error"] = traceback.format_exc()
            line("cupy CUDA version check", f"failed: {exc}")

        try:
            arr = cp.asarray(np.arange(64, dtype=np.float32).reshape(8, 8))
            out = cp.asnumpy((arr + 1).sum())
            RESULTS["cupy.array_smoke"] = f"pass: {float(out):.1f}"
            line("cupy array operation", RESULTS["cupy.array_smoke"])
        except Exception as exc:
            RESULTS["cupy.array_smoke"] = traceback.format_exc()
            line("cupy array operation", f"failed: {exc}")

        try:
            arr = cp.asarray(np.arange(64, dtype=np.float32).reshape(8, 8))
            eroded = ndi.grey_erosion(arr, size=(3, 3), mode="reflect")
            dilated = ndi.grey_dilation(eroded, size=(3, 3), mode="reflect")
            _ = cp.asnumpy(dilated)
            RESULTS["cupyx.grey_morphology_smoke"] = "pass"
            line("cupyx grey_erosion/grey_dilation", "pass")
        except Exception as exc:
            RESULTS["cupyx.grey_morphology_smoke"] = traceback.format_exc()
            line("cupyx grey_erosion/grey_dilation", f"failed: {exc}")
    except Exception as exc:
        RESULTS["cupy.import_error"] = traceback.format_exc()
        line("cupy import", f"failed: {exc}")


def diagnose_cucim():
    section("cuCIM")
    try:
        import cucim

        RESULTS["cucim.import"] = "pass"
        RESULTS["cucim.version"] = getattr(cucim, "__version__", "unknown")
        line("cucim import", "pass")
        line("cucim version", RESULTS["cucim.version"])
    except Exception as exc:
        RESULTS["cucim.import"] = traceback.format_exc()
        line("cucim import", f"failed: {exc}")
        return

    try:
        import cupy as cp
        from cucim.skimage import morphology

        arr = cp.asarray(np.zeros((16, 16), dtype=bool))
        arr[4:12, 4:12] = True
        out = morphology.binary_dilation(arr)
        _ = cp.asnumpy(out)
        RESULTS["cucim.morphology_smoke"] = "pass"
        line("cucim morphology operation", "pass")
    except Exception as exc:
        RESULTS["cucim.morphology_smoke"] = traceback.format_exc()
        line("cucim morphology operation", f"failed: {exc}")


def diagnose_tensorflow():
    section("TensorFlow / Mesmer GPU visibility")
    try:
        import tensorflow as tf

        RESULTS["tensorflow.version"] = tf.__version__
        devices = tf.config.list_physical_devices("GPU")
        RESULTS["tensorflow.gpu_devices"] = [str(d) for d in devices]
        line("tensorflow version", tf.__version__)
        line("TensorFlow GPU devices", RESULTS["tensorflow.gpu_devices"])
    except Exception as exc:
        RESULTS["tensorflow.import_or_gpu_error"] = traceback.format_exc()
        line("TensorFlow check", f"failed: {exc}")


def diagnose_cellpose():
    section("Cellpose")
    try:
        from cellpose import core, models

        RESULTS["cellpose.import"] = "pass"
        try:
            import cellpose

            RESULTS["cellpose.version"] = getattr(cellpose, "__version__", "unknown")
        except Exception:
            RESULTS["cellpose.version"] = "unknown"
        line("cellpose import", "pass")
        line("cellpose version", RESULTS["cellpose.version"])
        try:
            RESULTS["cellpose.core.use_gpu"] = core.use_gpu() if hasattr(core, "use_gpu") else "missing"
            line("cellpose.core.use_gpu()", RESULTS["cellpose.core.use_gpu"])
        except Exception as exc:
            RESULTS["cellpose.core.use_gpu"] = traceback.format_exc()
            line("cellpose.core.use_gpu()", f"failed: {exc}")
    except Exception as exc:
        RESULTS["cellpose.import"] = traceback.format_exc()
        line("cellpose import", f"failed: {exc}")
        return

    try:
        import torch

        requested_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        mem_before = None
        mem_after = None
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            mem_before = torch.cuda.memory_reserved(0)
        started = time.perf_counter()
        model = models.CellposeModel(device=requested_device)
        selected = str(getattr(model, "device", "unknown"))
        image = np.zeros((256, 256), dtype=np.float32)
        image[80:160, 96:176] = 1.0
        masks, _, _ = model.eval(image, diameter=30, do_3D=False)
        runtime = time.perf_counter() - started
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            mem_after = torch.cuda.memory_reserved(0)
        RESULTS["cellpose.model_device"] = selected
        RESULTS["cellpose.smoke"] = "pass"
        RESULTS["cellpose.smoke_runtime_seconds"] = runtime
        RESULTS["cellpose.smoke_gpu_mem_delta_mb"] = None if mem_before is None or mem_after is None else (mem_after - mem_before) / (1024 ** 2)
        line("cellpose model selected device", selected)
        line("cellpose 256x256 eval", f"pass runtime={runtime:.3f}s labels={int(np.asarray(masks).max())}")
        line("cellpose GPU memory delta MB", RESULTS["cellpose.smoke_gpu_mem_delta_mb"])
    except Exception as exc:
        RESULTS["cellpose.smoke"] = traceback.format_exc()
        line("cellpose 256x256 eval", f"failed: {exc}")


def main():
    diagnose_nvidia()
    diagnose_torch()
    diagnose_cupy()
    diagnose_cucim()
    diagnose_tensorflow()
    diagnose_cellpose()

    section("Diagnosis")
    likely = diagnose_likely_reason()
    fix = recommend_fix()
    RESULTS["diagnosis.likely_reason"] = likely
    RESULTS["diagnosis.recommended_fix"] = fix
    line("[DIAGNOSIS] likely_reason", likely)
    line("[DIAGNOSIS] recommended_fix", fix)
    line("[DIAGNOSIS] note", "No environment changes were made.")


if __name__ == "__main__":
    main()
