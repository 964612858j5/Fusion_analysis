# fusion_mesmer: the application's runtime environment

One micromamba environment holds the application and all three segmentation
engines (Cellpose, StarDist, Mesmer). Each engine runs in its own subprocess
of this environment (`seg_runner/`), so engines are isolated as processes, not
as dependency sets (plan 7.10).

## Files

| File | What it is |
|---|---|
| `conda-linux-64.lock` | conda part, explicit lock: exact package URLs and md5, linux-64 only (161 packages) |
| `requirements-pip.txt` | pip part: every distribution pip installed, exact versions, installed with `--no-deps` (219 packages; anndata + array-api-compat added for Step4 h5ad, block S4-2; pyarrow 25.0.1 for the object layer's Parquet tables, block A4 -- only `core/object_tables.py` imports it) |
| `requirements-pip-older-linux.txt` | the interface's Qt on Linux with glibc 2.28-2.33: `pyside6-essentials` and `shiboken6` 6.9.3, put by the installer in place of `requirements-pip.txt`'s two Qt lines (PyPI's 6.10+ wheels need glibc 2.34; docs/v17_older_linux_qt_plan.md) |
| `environment.yml` | human-readable export of both parts, for review and diffing only |
| `models.json` | model manifest: where each engine loads its weights from, size and SHA-256 of every file |

Regenerate all four from the live environment with
`scripts/export_fusion_mesmer_env.sh` and `python scripts/model_manifest.py --write`.

## Target machine

- Linux x86_64. The lock is platform-specific; no other platform is supported.
- glibc 2.28 or newer. Below 2.34 the installer builds the environment with
  PySide6 6.9.3 (`requirements-pip-older-linux.txt`) into
  `fusion_mesmer-qt69-<id>`; `--older-qt` does the same on a newer system.
  On RHEL/Rocky 8, `libxcb-cursor.so.0` (`xcb-util-cursor`) is in EPEL.
- **The code must keep running on PySide6 6.9.3.** Before a release, on the
  Mac or any test machine, put the two 6.9.3 wheels first on the path and
  run the tests (the environment itself is left alone):

  ```bash
  python -m pip install --no-deps --target /tmp/ps693 pyside6-essentials==6.9.3 shiboken6==6.9.3
  PYTHONPATH=/tmp/ps693:$PYTHONPATH QT_QPA_PLATFORM=offscreen python -m pytest tests
  ```

  Compare with the same run without `/tmp/ps693`: no test may fail only on
  6.9.3.
- An NVIDIA driver. This environment was built and verified with driver
  535.309.01 on an RTX 4090; older drivers are untested.
- **CUDA 12.2 headers at `/usr/local/cuda-12.2`.** cupy compiles the GPU
  background-correction kernels at run time against the system CUDA headers.
  With cupy 13.6.0 this failed against these headers (`cuda_fp8.h`:
  `__nv_bfloat16_raw` undefined) and the correction silently fell back to the
  CPU; the lock pins cupy 13.3.0, which works with them. A machine with other
  headers must be re-verified (step 4 below).
- `KERAS_BACKEND=tensorflow` (the application and `seg_runner` set it).

On the build machine, TensorFlow 2.8.4 sees no GPU. StarDist now runs its
network on PyTorch too (`seg_runner/stardist_torch`, docs/stardist_torch_plan.md):
the engine still builds the installed `StarDist2D` model for its configuration,
thresholds and prediction loop, but replaces its Keras network with the same
`weights_best.h5` converted to torch -- so it uses the GPU (CUDA, or the Mac's
MPS) like Cellpose and Mesmer, and TensorFlow's GPUs are hidden before the
Keras model is built. The conversion needs no TensorFlow:
`scripts/install_block01.sh` runs `scripts/convert_stardist_to_torch.py` once,
after the original models are verified and before the self-test, into the
directory of the manifest entry `stardist_2D_versatile_fluo_torch` (or
`BLOCK01_STARDIST_TORCH_PATH`); the engine checks it against the content digest
in `models.json`. `python scripts/convert_stardist_to_torch.py` does the same by
hand. An existing installation that upgrades to this version must run
`bash scripts/install_block01.sh` once more, with the same install root and
options: it converts the StarDist weights, and without them the StarDist
engine cannot start (the `.pt` is missing). Cellpose and Mesmer use the GPU
through PyTorch as before. Mesmer runs a PyTorch copy of the Keras model's
weights (`seg_runner/mesmer_torch`, docs/mesmer_torch_plan.md §10). Nothing
has to be done for it: at its first use on a machine (the
selftest below, or the first Mesmer run) the engine converts the Keras model
once, on the CPU (about 20 s), into the directory of the manifest entry
`mesmer_multiplex_segmentation_torch` (or BLOCK01_MESMER_TORCH_PATH), and
checks the result against the content digest in `models.json`. Later starts
load it directly. `python scripts/convert_mesmer_to_torch.py` does the same by
hand. The converted weights keep the original's non-commercial academic
licence: never commit or redistribute them.

## Install

**Use `bash scripts/install_block01.sh`** (docs/v17_install_plan.md): it does
the steps below plus the viewer core, the models (licence first) and the
checks, into its own root (`~/.local/share/block01`, or `--root DIR`):

| what | where |
|---|---|
| environment | `<root>/envs/fusion_mesmer-<8 hex of lock + pip file>` (+ `.owner` beside it, `.block01_install_ok` in it when finished); below glibc 2.34 `fusion_mesmer-qt69-<8 hex of lock + both pip files>`, its pip list beside it as `.requirements-pip.txt` |
| viewer core | the prebuilt wheel whose SHA-256 is in `native/fusion_viewer_core/wheels.lock`, or `--build-core` |
| models | `<root>/models/<models.json entry>/`, found through each entry's `env_override` variable |
| per checkout | `<repo>/.block01/install.env`, `pkg/block01` link, `run/` (working directory) |

`scripts/block01` starts the app from that record. The manual steps, for
reference (they do NOT install the viewer core, which neither manifest
lists):

```bash
micromamba create -y -p <prefix> --file envs/fusion_mesmer/conda-linux-64.lock
<prefix>/bin/python -m pip install --no-deps -r envs/fusion_mesmer/requirements-pip.txt
```

`scripts/rebuild_fusion_mesmer_env_check.sh <temp_prefix>` does the same into
a temporary prefix, checks the result against the lock and deletes it.

## Models

The weights are not in the repository (cpsam alone is 1.2 GB). Copy them to the
locations in `models.json` (`~` is the running user's home; the Mesmer path can
be overridden with `DEEPCELL_MESMER_MODEL_PATH`). The Mesmer PyTorch weights
are not copied: the selftest makes them (above).

## Verify

```bash
cd /tmp   # not the repository: CUDA libraries write cufile.log into the working dir
PYTHONPATH=<repo> <prefix>/bin/python -m seg_runner.selftest            # all three engines; makes the Mesmer PyTorch weights the first time
PYTHONPATH=<repo> unshare -n <prefix>/bin/python -m seg_runner.selftest # the same without network (root)
<prefix>/bin/python <repo>/scripts/model_manifest.py --verify          # every model file, after the selftest
```

Each engine must report `"state": "ok"` and `"exit_code": 0`.

## Verification record (2026-09-23, build machine)

- Rebuild from the lock into a temporary prefix: conda part identical (161),
  all 216 pip pins present, `pip check` clean; 3 min 20 s with warm package
  caches, 12 GB. The temporary prefix was deleted.
- Self-check, with and without network:

  | engine | device | model load | one 384×384 task | peak RSS | GPU memory |
  |---|---|---|---|---|---|
  | cellpose | cuda:0 | 6.1 s | 1.3 s | 1.8 GiB | 3.2 GiB |
  | stardist | cpu | 2.5 s | 1.3 s | 0.7 GiB | 0 |
  | mesmer | gpu (torch, 2026-10-25) | 1.7 s | 1.0 s (first task, cuDNN warm-up) | 0.8 GiB | 0.7 GiB |

  Before 2026-10-25 Mesmer ran on TensorFlow, on the CPU: 9.4 s load, 3.1 s
  per task, 1.4 GiB.

- Each engine's result in its subprocess equals the direct library call on the
  same input, pixel for pixel (`tests/test_seg_runner.py`); for Mesmer the
  direct call is the torch implementation on the same device.
- Cellpose 4.1.1 rewrites a multi-channel input array in place during `eval`
  (up to 0.04 on [0, 1] data). `seg_runner` hands it a copy; any other caller
  must not reuse an array it passed to Cellpose.
