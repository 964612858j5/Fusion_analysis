# Cellpose and Mesmer on the Mac's GPU (Apple MPS) — plan v1.2, 2026-11-09

v1.2 — user rulings (TG 2026-11-09): **GPU and CPU results may differ:
accepted** (reported, no pass line); **Mesmer is done in the same block**
(§2 M1). v1.1 took codex round 1 (§6).

Requested by the user (TG 2026-11-09: "对于cellpose的支持，我要求开发一个支持mac的gpu的版本"; "同意cellpose的方案"; segmentation tests wait for it). This is
the plan only; no code moves before the user approves it after review
(codex astra low).

## 1. Today (read-only check, HEAD b22cdcd + working tree)

* The Cellpose methods run in the engine process (`seg_runner`), started by
  Step2 (`workers/segment_merge_worker.py:2590`, `EngineProcess(engine,
  cpu_only=not use_gpu)`) and Step1 pre-segmentation, with the app's own
  Python (`seg_runner/client.py:39`).
* The engine picks its device as `models.CellposeModel(gpu=torch.cuda.is_available())`
  (`seg_runner/engines.py:182`): only NVIDIA (CUDA). On a Mac it is always
  the CPU, even though PyTorch and Cellpose support Apple's GPU (MPS).
* "Use GPU" off is enforced only by `CUDA_VISIBLE_DEVICES=""`
  (`seg_runner/client.py:56`), which does not hide MPS.
* The engine identity (whether results count as "the same segmentation") is
  engine kind + behaviour version + model (`engines.behavior_identity`);
  the device is provenance only (`runner.py:99`). A device change does not
  invalidate earlier results.
* This Mac mini (Apple M6, 16 GB unified memory) has no Cellpose and no
  PyTorch in its test env (`block01_mac`).

## 2. Changes (closed whitelist)

**E — test environment only (no repo change):** in `block01_mac`,
`pip install torch==2.5.1 torchvision==0.20.1 cellpose==4.1.1` (the product
pins without `+cu121`; macOS arm64 wheels have MPS), with a constraint file
holding numpy 1.26.4 / PySide6 6.11.3 / TensorFlow 2.15.1 so nothing already
installed moves (`pip freeze` before/after compared, `pip check`). The
`cpsam` model (1.23 GB, `envs/fusion_mesmer/models.json`) into
`~/.cellpose/models`, checked by `scripts/model_manifest.py --verify`.

**C1 — `seg_runner/engines.py`:** one function, `cellpose_model_args()`,
returns the keyword arguments for `models.CellposeModel`:
* CUDA available (and GPU wanted) → `{"gpu": True}` — EXACTLY today's call,
  so Cellpose's own device check (`assign_device`, which test-allocates and
  falls back to the CPU) still runs on NVIDIA machines;
* else MPS available (`torch.backends.mps.is_available()`) and GPU wanted →
  `{"gpu": True, "device": torch.device("mps")}` (Cellpose 4.1.1: an explicit
  device overrides `gpu`);
* else → `{"gpu": False}`.
`CellposeEngine.__init__` uses it; `self.device` stays what the model
reports ("cuda:0" / "mps" / "cpu"). Device strings and `torch.device`
objects are not compared with each other.

**M1 — `seg_runner/engines.py` `MesmerEngine.__init__`:** the same choice
through one helper, `torch_device()` → "cuda" / "mps" / "cpu" (CUDA first,
then MPS, CPU when neither or GPU not wanted); `weights.load(path, dev, ...)`
and `app.MesmerTorch(params, dev)` get it; `self.device` stays "gpu" on
CUDA (as today) and is "mps" on the Mac's GPU, "cpu" otherwise. The CUDA
numerics guard (`mesmer_torch/app.py:43`, cuDNN/TF32 switches) is left as is
(it does nothing on MPS). `cellpose_model_args()` uses the same helper.
Mesmer on the Mac needs its converted weights: first use converts them from
the DeepCell Keras model (`prepare.ensure`), which needs the DeepCell
download (a token); instead the converted weights can be copied from the
Ubuntu server's model root and checked against `models.json`
(`scripts/model_manifest.py --verify`) — the user's choice, §5.

**C2 — `seg_runner/client.py` `EngineProcess.start`:** `cpu_only` also sets
`BLOCK01_ENGINE_CPU_ONLY=1` in the engine's environment (kept:
`CUDA_VISIBLE_DEVICES=""`), and C1 reads it — so "Use GPU" off means the CPU
on a Mac too. `PYTORCH_ENABLE_MPS_FALLBACK=1` is set for the engine process
(an operation MPS lacks then runs on the CPU instead of failing).

**T — tests (closed list):**
* new `tests/test_cellpose_device.py`: `cellpose_model_args()` with
  `torch.cuda.is_available` / `torch.backends.mps.is_available` and the
  CPU-only variable faked (no model loaded): CUDA → `{"gpu": True}` only,
  MPS second, CPU last, CPU-only wins; `EngineProcess.start` puts
  `BLOCK01_ENGINE_CPU_ONLY` and `PYTORCH_ENABLE_MPS_FALLBACK` in the child's
  environment (checked on the `Popen` call, before any torch import).
* the same new module: `torch_device()` order and the Mesmer device label
  ("gpu" on CUDA unchanged, "mps", "cpu"), with availability faked.
* `tests/test_seg_runner.py:163` (`test_cellpose_in_subprocess_equals_direct_call`)
  and `tests/test_seg_runner_engines.py:70`
  (`test_cellpose_expansion_returns_the_nuclei_from_before_expanding`): the
  direct call builds its model with `cellpose_model_args()` too, so both
  sides run on the SAME device and stay pixel-equal; today they would
  compare MPS (subprocess) with CPU (direct) on a Mac. Nothing else in them
  changes.

**D — docs (closed list):** this plan's record; `KNOWN_ISSUES.md` if MPS
results differ (§3); `docs/user_guide.md` and `docs/用户指南.md`, one
sentence each: on a Mac with Apple silicon, when "Use GPU" is on and the
Mac's GPU (MPS) is available, Cellpose runs on it; otherwise on the CPU.

The Mesmer equality tests (`tests/test_seg_runner.py:182`, `:205`) already
compare on the same device (the engine object in-process / an explicit CPU
call with `cpu_only=True`) and are not changed; on MPS they show whether MPS
is repeatable.

Not changed: StarDist (the TensorFlow / PyTorch-port question, separate;
its verification runs on Ubuntu), the Step2 worker (its CUDA memory statistics simply do
not apply on MPS), the engine identity, the UI.

## 3. Verification

* **The device actually used** is part of every check: the engine's
  `hello.device` and each task's `device_used` (`seg_runner/runner.py:99`,
  `:119`) must say `mps` for the GPU runs and `cpu` for the CPU runs —
  similar results alone do not count.
* **Mac, same tiles, CPU vs MPS** (Cellpose GPU and CPU agree closely, not
  bit for bit): the 4 Step1 patches of the user's test project and one Step2
  tile, through the engine process (CPU = `cpu_only`). Measured per patch:
  cell counts; cells matched ONE-TO-ONE at IoU ≥ 0.5 (each cell at most one
  partner, best IoU first); the matched share on each side; mean IoU of the
  matched pairs; foreground pixel agreement; run time. **User ruling: a
  difference is accepted** — the numbers are reported, there is no pass
  line; only a broken run (an error, an empty result where the CPU found
  cells) counts as a failure. The same for Mesmer (both compartments).
* **Memory**: the engine process's resident memory (`ps` RSS, sampled each
  second) and, in the measurement script only, `torch.mps.driver_allocated_memory()`
  — named as such; neither is Activity Monitor's unified-memory figure.
* **Step1 (public path)**: a NEW pre-segmentation run with Cellpose on a
  patch (a reused earlier result proves nothing about the device;
  `core/preseg_run.py:217`, `:235`), its record showing `mps`.
* **Step2 (public path)**: "Use GPU" on → the engine log / run meta says
  `mps`; "Use GPU" off → `cpu`; and the segmentation result is looked at
  (Step3's masks over the slide) -- the device log alone is not the check.
* A real Step2 run on the user's test project copy with the Cellpose
  method on the Mac (this is the segmentation test the user is waiting for),
  and the Step2 skip-tiles run check (§8 item 5 of the skip-tiles plan).
* Ubuntu / CUDA (when the server is back): the CUDA branch is unchanged;
  the existing engine tests there.

## 4. Risks and rollback

* MPS may miss an operation Cellpose uses → the fallback variable runs it on
  the CPU; this covers missing operations only, not every MPS error, and
  does not by itself prove equal results (that is §3). Cellpose 4.1.1 uses
  bfloat16, which PyTorch 2.5.1 supports on MPS from macOS 14: the supported
  range is macOS 14 or newer on Apple silicon (this Mac qualifies); older
  systems get the CPU or an error, recorded, not worked around.
* Memory: unified memory is shared with the app; a tile that does not fit
  fails the run with the engine's error (as on CUDA). The Step2 tile size is
  the user's.
* Small result differences from CPU / CUDA (§3) — reported, ruled on.
* Rollback: revert C1/M1/C2; the test env keeps working on the CPU (Cellpose
  and Mesmer alike).

## 5. Settled / open

1. CPU vs MPS differences: accepted (user, 2026-11-09).
2. Mesmer: in this block (user, 2026-11-09).
3. Open: Mesmer's converted weights on the Mac — copied from the Ubuntu
   server (recommended), or downloaded with the user's DeepCell token.

## 6. Codex plan review round 1 (astra low) — FAIL (P3 only), taken in v1.1

* Two existing pixel-equality tests would compare MPS with CPU on a Mac →
  both now build the direct model with the same device arguments (T).
* Verification must prove the device: `hello.device` / `device_used`, a new
  Step1 run, Step2 Use GPU on/off (§3).
* Pass line defined (one-to-one matching, per patch, empty cases) and marked
  pending -- superseded in v1.2 by the user's ruling (differences accepted,
  reported only); memory measures named for what they are.
* Docs named file by file; wording conditional.
* Advisory taken: on CUDA the call stays exactly `gpu=True` (Cellpose's own
  device check unchanged); fallback limits and the macOS 14 requirement
  stated.

## 7. Codex plan review round 2 (astra low) — PASS

No P1/P2/P3; M1's device use is right (weights, input and generated
coordinates follow the device; output back to the CPU); CUDA unchanged.
Advisories taken: rollback names M1, §6's pass line marked superseded, the
Step2 check includes looking at the result. Hardening stops (two rounds).
User approvals: the Cellpose plan and Mesmer in the same block (TG
2026-11-09). Open: Mesmer weights on the Mac (§5.3).

## 8. Execution record (2026-11-10, Mac mini M6, macOS 27.2, 16 GB)

Test env (block01_mac) additions, nothing installed before changed
(`pip freeze` compared, `pip check` clean): torch 2.5.1 / torchvision
0.20.1 / cellpose 4.1.1 (+ fastremap, fill-voids, segment_anything);
deepcell 0.12.10 and deepcell-toolbox 0.12.1 with `--no-deps`; the cpsam
model (SHA-256 = models.json). Mesmer: the official MultiplexSegmentation-9
from DeepCell's public storage (docs/v17_mesmer_public_download_plan.md),
converted by the product's `prepare.ensure` (head differences <= 1.5e-6;
content digest = models.json's `content_sha256`).

Code: C1 / M1 (`torch_device()`, `cellpose_model_args()`, MesmerEngine on
the chosen device, "gpu"/"mps"/"cpu"), C2 (`BLOCK01_ENGINE_CPU_ONLY`,
`PYTORCH_ENABLE_MPS_FALLBACK`), the two Cellpose equality tests on the same
device, `tests/test_cellpose_device.py` (13), user guides.

Tests on the Mac: test_cellpose_device 13 passed; test_seg_runner +
test_seg_runner_engines, Cellpose and Mesmer cases: 10 passed, 1 skipped
(the TF-Mesmer comparison needs scikit-learn, not in the test env).
Mutations: MPS branch removed -> 2 device tests fail; the client's CPU-only
variable removed -> the client test fails.

CPU vs MPS through the engine process (Cellpose) and the Mesmer app on the
user's fused image (4 Step1 patches 512x512 + one 2048x2048 tile); one-to-one
matching at IoU >= 0.5; `hello.device` / `device_used` = mps / cpu checked:

| | cells (mps / cpu) | matched | mean IoU | time mps / cpu |
|---|---|---|---|---|
| Cellpose P1 | 392 / 392 | 100 % | 0.996 | 6.2 s (warm-up) / 156.7 s |
| Cellpose P2, P3, P4 | 305, 302, 369 both | 100 % | 0.997 | ~1.5 s / ~159 s |
| Cellpose 2048 tile | 4451 / 4452 | 99.9 % | 0.995 | 18.0 s / 1603 s |
| Mesmer 4 patches, both compartments | identical | 100 % | 1.000 | ~0.4 s / ~1.3 s |
| Mesmer 2048 tile | 4441 cells, 4478 nuclei | 100 % | 1.000 (1 px differs) | 6.2 s / 17.3 s |

Engine peak RSS: 893 MB (mps), 2580 MB (cpu). Differences accepted by the
user's ruling. Open: code review; the user's Step2 run in the app (with the
skip-tiles plan's item 5).

## 9. Code review round 1 (astra low) — FAIL, taken

* P2: the run's resource monitor knew "gpu"/"cuda"/"cpu" only, so a Mac GPU
  run was logged as "unknown" (Mesmer) or CPU (Cellpose). Scope extended
  with the user's approval (TG 2026-11-10): `utils/runtime_resource_monitor.py`
  `_engine_reported_device` counts "mps" as a GPU (one line);
  `tests/test_runtime_resource_monitor.py` checks "mps" for both engines
  (never a CPU fallback). No MPS memory statistics (not asked for).
* P3: `PYTORCH_ENABLE_MPS_FALLBACK` was `setdefault` (an explicit 0 in the
  parent survived); now set to 1, and the client test starts from 0.

## 10. Code review round 2 (astra low) — FAIL (one new P3), taken

P2 / P3 of round 1 confirmed taken. New P3: with "mps" a GPU, the report's
`actual_cuda_execution` (shown by `utils/step2_profiler.py:288`) became True
for a Mac GPU run; now False for "mps", CUDA unchanged; a test checks both.

## 11. Code review round 3 (astra low) — PASS

The round-2 P3 is closed: "mps" is a GPU but not CUDA execution; CUDA
("cuda:0", Mesmer's "gpu") unchanged. No new public-path defect; hardening
stops. Open: the user's Step2 run in the app (GPU on / off, the masks, and
the skip-tiles plan's item 5). Not committed.

## 12. User acceptance (TG 2026-11-10)

Real machine (Mac mini): Step2 with Cellpose runs on the Mac's GPU — accepted
("cellpose可以用"). Commit and push authorized.
