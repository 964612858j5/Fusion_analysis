#!/usr/bin/env python3
"""Diagnose StarDist CPU and Cellpose GPU runtime support.

This script only reports environment and backend test results.  It does not
install, uninstall, or modify packages.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import subprocess
import sys
import textwrap
import traceback


PACKAGES = [
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("skimage", "scikit-image"),
    ("tensorflow", "tensorflow"),
    ("stardist", "stardist"),
    ("csbdeep", "csbdeep"),
    ("torch", "torch"),
    ("cellpose", "cellpose"),
    ("cupy", "cupy"),
]


def _version(import_name: str, dist_name: str) -> str:
    try:
        return importlib.metadata.version(dist_name)
    except Exception:
        try:
            mod = importlib.import_module(import_name)
            return str(getattr(mod, "__version__", "installed, version unknown"))
        except Exception:
            return "not installed / not importable"


def print_basic_info() -> None:
    print("=" * 80)
    print("Part A - Basic environment info")
    print("=" * 80)
    print(f"Python executable: {sys.executable}")
    print(f"Python version:    {sys.version.replace(os.linesep, ' ')}")
    print(f"Platform:          {platform.platform()}")
    print(f"CONDA_DEFAULT_ENV: {os.environ.get('CONDA_DEFAULT_ENV', '')}")
    print(f"MAMBA_DEFAULT_ENV: {os.environ.get('MAMBA_DEFAULT_ENV', '')}")
    print(f"VIRTUAL_ENV:       {os.environ.get('VIRTUAL_ENV', '')}")
    print(f"CUDA_VISIBLE_DEVICES: {os.environ.get('CUDA_VISIBLE_DEVICES', '')}")
    print(f"LD_LIBRARY_PATH:      {os.environ.get('LD_LIBRARY_PATH', '')}")
    print()
    print("Installed versions:")
    for import_name, dist_name in PACKAGES:
        print(f"  {import_name:12s}: {_version(import_name, dist_name)}")
    print()


STARDIST_CPU_CODE = r"""
import os
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import traceback

try:
    import numpy as np
    import tensorflow as tf
    from stardist.models import StarDist2D
    from csbdeep.utils import normalize

    print("=" * 80)
    print("Part B - StarDist CPU test")
    print("=" * 80)
    print(f"CUDA_VISIBLE_DEVICES inside subprocess: {os.environ.get('CUDA_VISIBLE_DEVICES', '')!r}")
    print(f"TensorFlow version: {tf.__version__}")
    print(f"TensorFlow physical GPUs visible: {tf.config.list_physical_devices('GPU')}")

    model = StarDist2D.from_pretrained("2D_versatile_fluo")

    h, w = 256, 256
    yy, xx = np.mgrid[:h, :w]
    img = np.zeros((h, w), dtype=np.float32)
    spots = [(64, 64, 12), (82, 180, 15), (150, 118, 18), (190, 200, 11), (200, 45, 14)]
    for cy, cx, rad in spots:
        img += np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * rad ** 2)).astype(np.float32)
    img = normalize(img, 1, 99.8, axis=(0, 1)).astype(np.float32)

    masks, details = model.predict_instances(img)
    print("StarDist CPU prediction succeeded.")
    print(f"mask shape: {masks.shape}")
    print(f"mask dtype: {masks.dtype}")
    print(f"max label:  {int(masks.max())}")
    print(f"objects:    {int(masks.max())}")
    print("RESULT: PASS")
    raise SystemExit(0)
except Exception:
    print("StarDist CPU prediction failed.")
    print(traceback.format_exc())
    print("RESULT: FAIL")
    raise SystemExit(2)
"""


CELLPOSE_GPU_CODE = r"""
import os
import traceback

try:
    import numpy as np
    import torch
    import cellpose
    from cellpose import models

    print("=" * 80)
    print("Part C - Cellpose GPU test")
    print("=" * 80)
    print(f"CUDA_VISIBLE_DEVICES inside subprocess: {os.environ.get('CUDA_VISIBLE_DEVICES', '')!r}")
    print(f"torch version: {torch.__version__}")
    print(f"torch CUDA version: {torch.version.cuda}")
    cuda_ok = torch.cuda.is_available()
    print(f"torch.cuda.is_available(): {cuda_ok}")
    if cuda_ok:
        print(f"GPU count: {torch.cuda.device_count()}")
        print(f"GPU name:  {torch.cuda.get_device_name(0)}")
    else:
        raise RuntimeError("torch cannot see a CUDA-capable GPU")

    h, w = 256, 256
    yy, xx = np.mgrid[:h, :w]
    img = np.zeros((h, w), dtype=np.float32)
    spots = [(58, 60, 14), (88, 178, 18), (155, 120, 20), (196, 196, 13), (202, 50, 16)]
    for cy, cx, rad in spots:
        img += np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * rad ** 2)).astype(np.float32)
    vmax = float(img.max())
    if vmax > 0:
        img = img / vmax

    model = None
    init_errors = []
    for factory in (
        lambda: models.Cellpose(gpu=True, model_type="nuclei"),
        lambda: models.CellposeModel(gpu=True, model_type="nuclei"),
        lambda: models.CellposeModel(gpu=True),
    ):
        try:
            model = factory()
            print(f"Loaded model class: {model.__class__.__name__}")
            break
        except Exception as exc:
            init_errors.append(repr(exc))
    if model is None:
        raise RuntimeError("Could not initialize a Cellpose GPU model: " + " | ".join(init_errors))

    eval_errors = []
    masks = None
    for kwargs in (
        {"diameter": 30, "channels": [0, 0]},
        {"diameter": 30},
        {},
    ):
        try:
            result = model.eval(img, **kwargs)
            masks = result[0] if isinstance(result, tuple) else result
            break
        except Exception as exc:
            eval_errors.append(f"{kwargs}: {exc!r}")
    if masks is None:
        raise RuntimeError("Cellpose eval failed for all call signatures: " + " | ".join(eval_errors))

    masks = np.asarray(masks)
    print("Cellpose GPU prediction succeeded.")
    print(f"mask shape: {masks.shape}")
    print(f"mask dtype: {masks.dtype}")
    print(f"max label:  {int(masks.max())}")
    print(f"objects:    {int(masks.max())}")
    print("RESULT: PASS")
    raise SystemExit(0)
except Exception:
    print("Cellpose GPU prediction failed.")
    print(traceback.format_exc())
    print("RESULT: FAIL")
    raise SystemExit(2)
"""


def run_child(name: str, code: str, env: dict[str, str]) -> bool:
    print(f"Launching {name} subprocess...")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print("\n[stderr]")
        print(proc.stderr.rstrip())
    print(f"{name} subprocess exit code: {proc.returncode}")
    print()
    return proc.returncode == 0


def main() -> int:
    print_basic_info()

    stardist_env = os.environ.copy()
    stardist_env["CUDA_VISIBLE_DEVICES"] = ""
    stardist_pass = run_child("StarDist CPU", STARDIST_CPU_CODE, stardist_env)

    cellpose_env = os.environ.copy()
    cellpose_pass = run_child("Cellpose GPU", CELLPOSE_GPU_CODE, cellpose_env)

    print("=" * 80)
    print("Part D - Summary")
    print("=" * 80)
    print(f"StarDist CPU: {'PASS' if stardist_pass else 'FAIL'}")
    print(f"Cellpose GPU: {'PASS' if cellpose_pass else 'FAIL'}")
    print()

    if stardist_pass and cellpose_pass:
        print("Recommended next action: environment looks ready for StarDist CPU and Cellpose GPU tests.")
    else:
        print("Recommended next action:")
        if not stardist_pass:
            print("  - Review the StarDist/TensorFlow traceback above; CPU isolation or model loading failed.")
        if not cellpose_pass:
            print("  - Review the Cellpose/torch traceback above; CUDA visibility, model initialization, or eval failed.")
        print("  - Do not change GUI code until the failing backend is fixed in the environment.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Diagnostic script failed unexpectedly.")
        print(traceback.format_exc())
        raise SystemExit(0)
