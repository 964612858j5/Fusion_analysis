# v17 CI — plan v2.1 (2026-11-01)

v2.1: round 2 — 2 P2 (freeze entry of the local wheel; exit 5 is not
"all skipped") applied in §2.2 / §2.2a.
v2: codex plan review round 1 (3 P2 + 2 P3, no P1) answered — see §7.

Source: whole-code review 2026-10-31 (codex finding 6, P2: "CI does not
protect the current branch or most of the important behavior"). User (TG
2026-11-01): next item; plan, codex review, then authorization.

## 0. Today

* One workflow, `.github/workflows/fusion_viewer_core.yml`: push / PR on
  `v16_5` only (paths `native/**` …). It builds the Rust wheel on Linux and
  Windows and runs `tests/test_v16_5_r1_ci.py` against the installed wheel
  (decoders, redraw notice). It never runs on `v17`; `cargo test` is never
  run; no other Python test runs anywhere but on this machine, by hand.
* The repo is public on GitHub (`964612858j5/Fusion_analysis`); Actions are
  enabled (Dependabot runs on `v17` succeed); runners: 4 vCPU, no GPU, no
  display.

## 1. Measured (2026-11-01, this machine)

* `cargo test --lib`: 110 passed, 1 ignored, ~15 s warm.
* A clean Python 3.10 venv with only CPU packages (pinned to the versions in
  `envs/fusion_mesmer/requirements-pip.txt`: numpy, scipy, zarr,
  numcodecs, tifffile, imagecodecs, pandas, scikit-image, anndata, h5py,
  pyarrow, openpyxl, opencv-python-headless, numba, PyQt5, pyqtgraph,
  PyOpenGL, psutil, roifile, ome-types, pytest) plus the locally built
  `fusion_viewer_core` wheel; no torch / TensorFlow / StarDist / Cellpose /
  cupy / cucim; `QT_QPA_PLATFORM=offscreen`, no `DISPLAY`,
  `CUDA_VISIBLE_DEVICES=`; one process per module, 4 in parallel:
  * 340 modules: **291 pass** (sum of module times 1721 s, i.e. about
    8 min wall at 4 in parallel), 32 skip whole (22 exit 5, 10 exit 0
    with every case skipped: GL / GPU / engines), 16 fail, 1 times out
    (`test_v16_rm2_chain.py`, > 600 s).
  * The 16 failures: 12 are the known pre-existing failures (backlog list:
    shared intensity lifecycle, real load first frame, preview source
    provider, compare channel state, correction param inheritance,
    intensity window, source-aware conditioning, Step0→Step1 handoff
    contract, compare tiles, Step1 channel state contract, Step1 live
    controls, Step1 overlay preview); 4 depend on what this env lacks
    (`test_step0_floor_prefetch` / `test_step0_method_prefetch`: cucim's
    floor; `test_step2_runner_path`: Cellpose; `test_global_channel_dock`:
    a font-dependent pixel size).
  * Stability: the 291 were run twice more, each a complete separate pass
    (§6): all 291 passed both times. A failure seen later is classified
    (flaky / environment / real) before a module leaves the list.

## 2. Proposal

### 2.1 `fusion_viewer_core.yml` (existing) — moved to v17, plus `cargo test`

* triggers: push / PR on `v17` (instead of `v16_5`, which is frozen), same
  paths; concurrency group `${{ github.workflow }}-${{ github.ref }}`;
* a step `cargo test --lib --manifest-path native/fusion_viewer_core/Cargo.toml`
  after `setup-python` (so PyO3 finds that interpreter) and before building
  the wheel, on both Linux and Windows (pinned toolchain 1.99.0 as today;
  `extension-module` is only in `pyproject.toml`, so plain `cargo test`
  links; PyO3 0.29 uses raw-dylib on Windows).

### 2.2 New `block01_cpu_tests.yml` — the Python lane

* triggers: push / PR on `v17`, all paths except `docs/**` and `**/*.md`;
  `workflow_dispatch` for a manual run.
* `ubuntu-24.04` (explicit, not `-latest`, which is moving to 26.04),
  Python 3.10, pinned Rust 1.99.0; system packages Qt needs offscreen
  (`libegl1 libxkbcommon0 libgl1 libfontconfig1 libdbus-1-3 libglib2.0-0
  libx11-6 libxext6`); job `timeout-minutes: 45`.
* build the wheel with maturin (as 2.1) and install it with the packages of
  a new file `envs/ci/requirements-cpu.txt` that pins EVERY package of the
  measured trial venv exactly (its `pip freeze`, incl. `PyQt5-Qt5`,
  `PyQt5-sip`, pytest), so CI runs what was measured — EXCEPT the
  `fusion_viewer_core @ file:///…` line, which is left out: the wheel built
  from the current checkout is installed separately (`--no-deps`). The final stability
  check (§4) is run with exactly that file and the final runner.
* a startup check before the tests: an offscreen `QApplication` starts,
  and the Qt version and font families are printed into the log.
* the checkout is reached as the package `block01` through a symlink
  (`mkdir -p "$RUNNER_TEMP/pkg" && ln -s "$GITHUB_WORKSPACE" "$RUNNER_TEMP/pkg/block01"`,
  absolute `PYTHONPATH`), as on this machine
  (`/sda1/Fusion/a9_bench/block01`). `tests/conftest.py` stays enabled
  (its Mesmer fixture only links weights that exist).
* No pip / cargo caches (no new cache without authorization); the 20 min
  target is measured cold.
* Concurrency: group `${{ github.workflow }}-${{ github.ref }}`,
  `cancel-in-progress: true` (the two workflows never cancel each other).
* every module's log is uploaded as an artifact with `if: always()`.

### 2.2a The runner `scripts/ci_run_modules.sh`

`ci_run_modules.sh PYTHON LIST TESTS_DIR LOG_DIR [JOBS=4]`:
* refuses (exit 2) an empty or missing list, and any listed module that
  does not exist;
* runs each module as its own `pytest -q -p no:cacheprovider` process,
  `JOBS` at a time, under `timeout -k 30 600` (TERM after 600 s, KILL 30 s
  later), output written straight to `LOG_DIR/<module>.log`;
* the environment is set, not inherited: `QT_QPA_PLATFORM=offscreen`,
  `CUDA_VISIBLE_DEVICES=`, `BLOCK01_REQUIRE_STEP1_GPU=0`, `DISPLAY` unset;
* each module's exit status is recorded (`LOG_DIR/status.txt`); a listed
  module must RUN something: pytest exit 5 (nothing collected) is a
  failure, and so is exit 0 with every collected case skipped — detected
  from the module's JUnit file (`--junitxml=LOG_DIR/<module>.xml`: tests
  minus skipped == 0), which covers collection-time (`importorskip`) and
  fixture-time skips alike; the runner exits non-zero
  when any module did not exit 0, and prints the failing ones with the
  last lines of their logs.
* validated before commit: a temporary list with a failing module between
  passing ones → non-zero exit; a module whose cases all skip (one via a
  module-level `importorskip`, one via a fixture `pytest.skip`) → non-zero;
  a module collecting nothing → non-zero; a missing
  module → exit 2; a module sleeping past a small test timeout → killed,
  non-zero.

The list is explicit (a closed list, AGENTS.md style), not "everything
that happens to pass": a new test module joins it by being added to the
file in the same commit; a known failure leaves it only with a note in this
plan.

### 2.3 What stays local (documented, not in CI)

* GL / GPU modules (22 skip here), StarDist / Cellpose / Mesmer engines,
  real displays and timing: still the local 4-group regression with
  `DISPLAY=:1 … BLOCK01_REQUIRE_STEP1_GPU=1`. CI passing is not real-machine
  acceptance (AGENTS.md).
* No Windows Python lane yet (the native Windows GUI work comes later; the
  Rust wheel already builds and is tested on Windows in 2.1).

## 3. Scope (closed whitelist)

* `.github/workflows/fusion_viewer_core.yml` (branches, cargo test step)
* `.github/workflows/block01_cpu_tests.yml` (new)
* `envs/ci/requirements-cpu.txt` (new)
* `tests/ci_cpu_modules.txt` (new): the 291, plus a comment block naming
  the 16 failing + 1 timing-out modules left out and why (12 known
  pre-existing, 4 environment, 1 timeout)
* `scripts/ci_run_modules.sh` (new)
* this plan

No product code, no test code changes. No new cache / scheduler / state
machine.

## 4. Acceptance

* The first push after the commit runs both workflows on `v17`; both green
  (links reported to the user). A deliberately broken commit is NOT pushed
  to check red — instead `ci_run_modules.sh` is shown failing locally on a
  temporary edit, then reverted.
* Time: Python lane under ~20 min wall on a GitHub runner; if much slower,
  the slowest modules are reported for a decision (not dropped silently).

## 5. Open questions for review (answered: v17 push + PR only; known failures stay out, named)

1. Push-only on `v17`, or also `main`? (Proposal: `v17` only; `main` is
   not where work happens.)
2. Should the 12 known pre-existing failures run in CI as "expected to
   fail" (xfail list) so a fix is noticed, or stay out? (Proposal: stay
   out; they are on the backlog.)

## 6. Stability runs

Trial venv (`pip freeze` = the planned `requirements-cpu.txt`), the trial
runner, 4 in parallel, two complete separate passes over the 291 after the
first: pass 2 — 291 / 291 exit 0; pass 3 — 291 / 291 exit 0. (One more pass
with the final runner and requirements file is part of §4 before commit.)

## 7. Codex plan review round 1 — answers

* P2 runner swallowed failures → §2.2a: per-module status, aggregation,
  exit 5 = failure, list / path checks, validated with synthetic failures.
* P2 timeout and logs → `timeout -k 30 600`, job timeout 45 min, logs to
  files, artifacts `if: always()`.
* P2 env not the measured one → `requirements-cpu.txt` = the trial's full
  freeze (Qt 5.15.19, SIP 12.20.0, pytest 9.2.0 as measured), final check
  with that file.
* P3 accounting → 32 wholly skipped (22 exit 5 + 10 exit 0); §6 filled
  with two complete passes; failures classified before exclusion.
* P3 env normalisation → runner sets `BLOCK01_REQUIRE_STEP1_GPU=0`, unsets
  `DISPLAY`.
* Also: `ubuntu-24.04` explicit, startup Qt check, concurrency group per
  workflow + ref, no caches, `**/*.md`.

## 8. Implementation notes (authorized 2026-11-01)

* `envs/ci/requirements-cpu.txt`: the trial venv's freeze (51 pins) minus
  the local `fusion_viewer_core` line. A fresh venv from it reproduces the
  freeze exactly (`pip freeze` diff empty).
* `scripts/ci_run_modules.sh` as §2.2a, plus `--rootdir=TESTS_DIR` (without
  it pytest walked up from a module outside the repo and failed on an
  unreadable `/tmp/fuse`). Validated with synthetic modules (limit 5 s):
  2 passing → 0; a failure between passing ones → 1 (rc 1 recorded);
  module-level `importorskip` → 1 (rc 5); fixture `pytest.skip` → 1
  (rc 105 = ran nothing); a module with no test → 1 (rc 5); a 60 s test →
  1 (rc 124); a missing module → 2; an empty list → 2.
* `tests/ci_cpu_modules.txt`: the 291, a header naming the 17 left out and
  why.
* Workflows as §2.1 / §2.2 (YAML parsed locally). The startup step also
  imports `block01` and `fusion_viewer_core`.
* Final local check (§4): fresh venv from `requirements-cpu.txt` + the
  wheel, the final runner, 4 jobs, the package through the
  `/sda1/Fusion/a9_bench` symlink: **all 291 modules passed, 7 min wall**.
