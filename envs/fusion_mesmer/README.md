# fusion_mesmer: the application's runtime environment

One micromamba environment holds the application and all three segmentation
engines (Cellpose, StarDist, Mesmer). Each engine runs in its own subprocess
of this environment (`seg_runner/`), so engines are isolated as processes, not
as dependency sets (plan 7.10).

## Files

| File | What it is |
|---|---|
| `conda-linux-64.lock` | conda part, explicit lock: exact package URLs and md5, linux-64 only (161 packages) |
| `requirements-pip.txt` | pip part: every distribution pip installed, exact versions, installed with `--no-deps` (216 packages) |
| `environment.yml` | human-readable export of both parts, for review and diffing only |
| `models.json` | model manifest: where each engine loads its weights from, size and SHA-256 of every file |

Regenerate all four from the live environment with
`scripts/export_fusion_mesmer_env.sh` and `python scripts/model_manifest.py --write`.

## Target machine

- Linux x86_64. The lock is platform-specific; no other platform is supported.
- An NVIDIA driver. This environment was built and verified with driver
  535.309.01 on an RTX 4090; older drivers are untested.
- **CUDA 12.2 headers at `/usr/local/cuda-12.2`.** cupy compiles the GPU
  background-correction kernels at run time against the system CUDA headers.
  With cupy 13.6.0 this failed against these headers (`cuda_fp8.h`:
  `__nv_bfloat16_raw` undefined) and the correction silently fell back to the
  CPU; the lock pins cupy 13.3.0, which works with them. A machine with other
  headers must be re-verified (step 4 below).
- `KERAS_BACKEND=tensorflow` (the application and `seg_runner` set it).

On the build machine, TensorFlow 2.8.4 sees no GPU: StarDist and Mesmer run on
the CPU. Cellpose uses the GPU through PyTorch.

## Install

```bash
micromamba create -y -p <prefix> --file envs/fusion_mesmer/conda-linux-64.lock
<prefix>/bin/python -m pip install --no-deps -r envs/fusion_mesmer/requirements-pip.txt
```

`scripts/rebuild_fusion_mesmer_env_check.sh <temp_prefix>` does the same into
a temporary prefix, checks the result against the lock and deletes it.

## Models

The weights are not in the repository (cpsam alone is 1.2 GB). Copy them to the
locations in `models.json` (`~` is the running user's home; the Mesmer path can
be overridden with `DEEPCELL_MESMER_MODEL_PATH`), then:

```bash
<prefix>/bin/python scripts/model_manifest.py --verify
```

## Verify

```bash
cd /tmp   # not the repository: CUDA libraries write cufile.log into the working dir
PYTHONPATH=<repo> <prefix>/bin/python -m seg_runner.selftest            # all three engines
PYTHONPATH=<repo> unshare -n <prefix>/bin/python -m seg_runner.selftest # the same without network (root)
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
  | mesmer | cpu | 9.4 s | 3.1 s | 1.4 GiB | 0 |

- Each engine's result in its subprocess equals the direct library call on the
  same input, pixel for pixel (`tests/test_seg_runner.py`).
- Cellpose 4.1.1 rewrites a multi-channel input array in place during `eval`
  (up to 0.04 on [0, 1] data). `seg_runner` hands it a copy; any other caller
  must not reuse an array it passed to Cellpose.
