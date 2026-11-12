# Step4 crashes on the Mac when numba compiles in its thread (backlog 14) — plan v0.2

Reported by the user (TG) 2026-11-11: after accepting backlog 12 and 13 in
Step0-Step3, starting Step4's extraction closed the whole GUI.

## 1. Cause (found and reproduced)

* macOS crash report `python3.10-2026-11-11-062858.ips` (20:50): `EXC_BAD_ACCESS
  / KERN_PROTECTION_FAILURE`, "Could not determine thread index for stack
  guard region", Bus error 10, in thread **`FeatureExtractWorker`**, inside
  `libllvmlite` (`MCJIT::finalizeObject` → `AsmPrinter::doFinalization` →
  `operator new`): a **stack overflow while numba's LLVM compiles** a kernel.
* `workers/feature_extract_worker.py:608` `FeatureExtractWorker(QThread)`
  runs `core/quant_engine.py`'s numba kernels (`@njit(..., cache=True)`,
  `_numba_kernel`, `_dist_kernels`). A QThread gets the platform's default
  stack: **512 KB on macOS** (8 MB on Linux, where this ran so far). LLVM's
  compile needs more.
* It shows only when numba has to compile (cold cache): its cache lives in
  the repo's `__pycache__`, which the clean-up of 2026-11-11 (backlog 10)
  removed. Any fresh install on a Mac would hit it the same way.
* **Reproduced** (scratchpad `nb/compile_in_thread.py`): the quant tests'
  kernels called inside a QThread with an empty `NUMBA_CACHE_DIR` →
  exit 138 (SIGBUS) with the default stack; exit 0 with a 32 MB stack.
* Stop-gap done: numba's cache rebuilt on the main thread (the quant
  tests, 35 pass, 24 cache files). It covers the signatures the tests use;
  another pixel type in the GUI can still compile in the thread.

## 2. Fix

`FeatureExtractWorker.__init__` sets its thread's stack before it starts:
`self.setStackSize(STACK_BYTES)`, `STACK_BYTES = 32 MiB` (virtual; pages
are used only as touched). The batch dialog (`ui/batch_step4_dialog.py:491`)
and the Step4 page (`ui/step4_page.py:701`) both build this class, so both
get it. Nothing else changes.

## 3. Files (closed list)

* `workers/feature_extract_worker.py`: `STACK_BYTES`, one line in
  `__init__`.
* Tests (new): `tests/test_step4_worker_stack.py`.
* This plan.

## 4. Tests

1. `FeatureExtractWorker(...).stackSize() == STACK_BYTES` (≥ 32 MiB).
2. In a subprocess with an empty `NUMBA_CACHE_DIR` and a timeout: the
   quant kernels (uint8, uint16 and float32 channels) compiled and run
   inside a QThread with `STACK_BYTES`; the thread reports an explicit
   success after `wait()`, an exception in it makes the subprocess exit
   non-zero. (With
   the default stack the same script exits 138 on the Mac: recorded in §1,
   not a test, because it crashes the process by design.)
3. Existing Step4 worker / quant tests: no new failures against HEAD.

Acceptance (user, Mac): Step4 extraction on the demo project runs to the
end; then once more in a **fresh GUI process started with an empty
`NUMBA_CACHE_DIR`** (cold numba cache; deleting files under a running GUI
would not do, its compiled kernels stay in memory).

## 5. Risks and rollback

* 32 MiB of address space per running extraction thread (one at a time).
* Rollback: revert the commit.

## 6. Plan review (codex astra low)

Round 1 PASS (scratchpad `codex_g14_plan_r1.txt`): compilation happens on
the worker's own thread (numba's pool threads only run compiled code);
`quant_apply` imports no kernel; SpatialData runs in its own interpreter;
both Step4 entries build this class. Three P3 test advisories, taken in
v0.2: the subprocess test checks an explicit success and propagates
exceptions, with a timeout; uint16 is covered; the cold acceptance uses a
fresh process with an empty `NUMBA_CACHE_DIR`.

## 7. Implementation (uncommitted, 2026-11-11)

User (TG): "同意批准第14项". `workers/feature_extract_worker.py`:
`STACK_BYTES = 32 MiB`, `setStackSize` right after `super().__init__()`.
`tests/test_step4_worker_stack.py` (2) pass; the same subprocess script
without the larger stack dies with SIGBUS (checked). Code review (codex
astra low) round 1 PASS, no findings.
