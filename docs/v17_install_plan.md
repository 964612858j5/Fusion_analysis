# v17 One-command install, versioned viewer core, launcher — plan v2.2 (2026-11-01) — codex round 4: READY

v2.2: codex round 3 (3 P2 + 1 P3, no P1) — see §9.

Later addition (2026-11-02): step 3b, the SpatialData export environment —
`docs/v17_spatialdata_env_install_plan.md`.
v2.1: codex round 2 (4 P2 + P3, no P1; user choices kept) — see §8.

v2: codex plan review round 1 (8 P2 + P3 fact corrections, no P1) and the
user's answers to §4 of v1 (TG 2026-11-01: **prebuilt viewer core**,
**model download after the licence is shown**, **AI playbook now, in-app
assistance later**) — see §7.

Source: whole-code review 2026-10-31 (codex finding 7, P2: "the documented
environment rebuild does not recreate the default viewer installation";
recommendation 9: "deliver and test a versioned application / native-wheel
pair from a clean installation"). User (TG 2026-11-01): wants one-click
deployment, optionally AI-assisted with the user's own API key.

## 0. Today (verified; corrected per codex round 1)

* `envs/fusion_mesmer/README.md` installs the conda lock +
  `requirements-pip.txt` (which needs PyTorch's wheel index, line 2) and has
  the model weights copied by hand. Neither manifest contains
  `fusion_viewer_core`; `scripts/rebuild_fusion_mesmer_env_check.sh` (a
  rebuild-into-a-new-prefix check that refuses an existing prefix) installs
  only those. Rust is the default viewer; a missing core falls back to the
  Python viewer with a log warning only (`ui/step1_core_adapter.py:47-51`);
  the viewer notice (`ui/step1_viewer_mount.py:1561`) does not show the
  reason.
* `core_unavailable_reason("0.1.0")` accepts any core >= 0.1.0; the crate is
  `0.1.0` since R0 (`Cargo.toml:3`, `Cargo.lock`), so R0 — without today's
  `ViewerCore` — passes the same check.
* Independent core users: Step0's viewer goes through the adapter
  (`ui/step0/engine_mount.py:73`); fusion execution imports the core itself
  and only checks for `FuseJob` (`ui/step0/overview_panel.py:586`); shutdown
  calls `wait_closing` directly (`utils/closing_threads.py:37`).
* `README.md` Quick start and the user guides are stale (`conda env create -f
  ../environment.yml`, env `fusion`). The working launch is
  `cd /sda1/Fusion/a9_bench && /root/micromamba/envs/fusion_mesmer/bin/python -m block01.main`
  (package importable as `block01` through a symlink).
* Models (`envs/fusion_mesmer/models.json`): cpsam under
  `~/.cellpose/models` (`CELLPOSE_LOCAL_MODELS_PATH` overrides; Cellpose
  downloads it from Hugging Face on first use), StarDist under `~/.keras`
  (csbdeep downloads it from GitHub on first use), Mesmer Keras at an
  absolute server path (`DEEPCELL_MESMER_MODEL_PATH`; obtained from DeepCell
  with an account token; non-commercial academic licence), Mesmer torch
  generated from it at first use (`BLOCK01_MESMER_TORCH_PATH`, checked by a
  content digest).
* CI builds the wheel on Linux and Windows on `native/**` changes and keeps
  it as a short-lived workflow artifact; no versioned release exists. The
  Linux wheel is built on ubuntu-latest without a manylinux container, so
  it would require that runner's glibc.
* `seg_runner.selftest` exits non-zero only on `state`, not on `exit_code`
  (`seg_runner/selftest.py:79`).
* CuPy 13.3 finds CUDA through `CUDA_PATH`, then `nvcc` on `PATH`, then
  `/usr/local/cuda`; the working server sets `/usr/local/cuda-12.2`.
* Linux x86_64 + NVIDIA only (linux-64 lock).

## 1. Viewer core: versioned, prebuilt, verified

### 1.1 Version and interface number

* `CORE_API: int` exported from the Python module (`src/py/mod.rs`), value
  `1`; raised on any incompatible change of the Python-visible interface.
* Crate version `0.2.0` now; every released binary gets a new version
  (also implementation-only fixes). Rule written in
  `native/fusion_viewer_core/README.md` (whose R0-era text is refreshed).
* `ui/step1_core_adapter.py`: `REQUIRED_CORE_API = 1`,
  `MIN_CORE_VERSION = "0.2.0"`; `core_unavailable_reason()` refuses a
  missing module, a missing or different `CORE_API`, or an older version;
  a newer compatible version is accepted. The one check is used by the
  viewer mount (as today), Step0's engine (as today, through
  `engine_refusal`), **fusion execution** (`overview_panel.py`: refused →
  the existing Python fusion path, reason logged) and **shutdown**
  (`closing_threads.py`: refused → skip `wait_closing`).
* The refusal reason reaches the user: the existing viewer notice shows the
  adapter's reason when Rust was requested and refused (one line, no new
  widget) — **for authorization** (UI text change).

### 1.2 Release mechanism (user choice: prebuilt) — new, for authorization

* NEW workflow `.github/workflows/fusion_viewer_core_release.yml`, run on a
  tag `core-v<version>` (and `workflow_dispatch` for a dry run without
  publishing):
  * on a tag: the tag with `core-v` stripped must equal `Cargo.toml`'s
    version, else fail; a `workflow_dispatch` dry run (any branch) builds
    and tests but never publishes;
  * Linux: maturin in a **manylinux_2_28** container, `-i python3.10`,
    `--compatibility manylinux_2_28` enforced (runs on glibc ≥ 2.28, e.g.
    Ubuntu 20.04+, RHEL 8+); Windows: as today; `--locked` (committed
    `Cargo.lock`), Rust 1.99.0;
  * each wheel installed into a clean venv and `test_v16_5_r1_ci.py` +
    a `CORE_API` check run, as the existing workflow does;
  * `SHA256SUMS` computed; a GitHub Release `core-v<version>` created with
    both wheels + `SHA256SUMS`.
* Trust: the installer does not trust the release's own checksum file. The
  digests are committed to the repo in `native/fusion_viewer_core/wheels.lock`
  (version, file name, SHA-256 per platform) in the commit that follows a
  release; the installer verifies the download against the checkout's
  `wheels.lock` over HTTPS and refuses on mismatch.
* Pushing a tag publishes: each release needs the user's go, like a push.
* Fallback when the checkout's version has no `wheels.lock` entry (a
  development checkout): `--build-core` builds from the checkout (pinned
  rustup toolchain under the install root, `rustup-init` downloaded over
  HTTPS from `static.rust-lang.org` with its published SHA-256 checked,
  `--no-modify-path --default-toolchain none`, then `1.99.0`; maturin
  pinned in a separate build venv; `--locked`; preflight checks `cc` /
  linker). Without `--build-core` the installer stops and says so.

## 2. `scripts/install_block01.sh`

```
bash scripts/install_block01.sh [--root DIR] [--check-only] [--build-core]
                                [--models DIR] [--accept-model-licences]
```

Install root default `~/.local/share/block01` (`--root` overrides). Plain
bash, `set -euo pipefail`, quoted paths; every step prints what it does and
on failure the step, the exact failing command and what to do.

1. **Preflight** (writes nothing): Linux x86_64, glibc ≥ 2.34 (was 2.28; raised
   2026-11-08 because PySide6 6.10+ ships Linux wheels only as
   manylinux_2_34, docs/v17_pyside6_wrapup_plan.md), `nvidia-smi`
   and driver (warn below the verified 535), `tar`/`bzip2`/`curl`; free
   space on each filesystem that receives the env (~12 GB), package caches,
   models (~1.4 GB); HTTPS reachability of conda-forge, PyPI, the PyTorch
   index, GitHub releases, Hugging Face, `users.deepcell.org`, and (for
   `--build-core`) the Rust endpoints;
   CUDA: which toolkit CuPy would use (`CUDA_PATH`, `nvcc`, `/usr/local/cuda`)
   and its version, recorded for 2.7. `--check-only` prints the report and
   stops.
2. **micromamba**: the one on `PATH`, else a pinned release downloaded over
   HTTPS into `<root>/bin` and verified against a digest pinned in the
   script before it is run. `MAMBA_ROOT_PREFIX=<root>/mamba` for the
   installer's own runs (never the user's existing root).
3. **Environment** at `<root>/envs/fusion_mesmer-<lock8>` (`lock8` = first
   8 hex of the lock + pip requirements digest):
   * absent → create from the lock, `pip install --no-deps -r` (with the
     PyTorch index), `pip check`; a marker `.block01_install_ok` is written
     only after all of section 2 succeeded;
   * present with the marker and matching manifests + core → nothing to do
     ("already installed");
   * ownership is recorded BEFORE the prefix exists: the installer writes
     the sibling file `<root>/envs/fusion_mesmer-<lock8>.owner` (pid, time,
     checkout) only when the prefix path does not exist yet, then runs
     `micromamba create`. Present without the ok marker but with its
     `.owner` file → an interrupted earlier run (also one killed in the
     middle of `micromamba create`): removed and recreated, after saying
     so. A prefix without an `.owner` file is never stamped or touched;
   * present and not created by this installer, or differing → refused,
     with "install into a new location: --root NEW_DIR". A prefix the
     installer did not create is never modified or deleted.
4. **Viewer core**: from `wheels.lock` (1.2) or `--build-core`; installed
   `--no-deps` into the env; `CORE_API` and version checked against the
   checkout.
5. **Models** (user choice: download after the licence is shown). One path
   contract for acquisition, loading, verification and the launcher:
   each original model lives in `<root>/models/<manifest entry>/`, and every
   entry has an `env_override` variable naming that directory:
   * `cellpose_cpsam` → `CELLPOSE_LOCAL_MODELS_PATH` (Cellpose's own
     variable: it loads `<dir>/cpsam` and downloads nothing when the file is
     there) — added to `models.json`;
   * `stardist_2D_versatile_fluo` → NEW `BLOCK01_STARDIST_MODEL_PATH` —
     added to `models.json`; `StardistEngine` loads
     `StarDist2D(None, name=<dir name>, basedir=<parent>)` from that folder
     when the variable is set (no `get_file`, no extraction, no `~/.keras`);
     unset → `from_pretrained` exactly as today (this server unchanged);
   * Mesmer Keras → `DEEPCELL_MESMER_MODEL_PATH`, Mesmer torch →
     `BLOCK01_MESMER_TORCH_PATH` (as today);
   * `model_root()` already resolves `env_override`, so
     `model_manifest.py --verify`, the engines and the converter all read
     the same directories; `scripts/model_manifest.py`'s `MODELS` table
     gains the same two `env_override` values so `--write` resolves and
     keeps them.

   The installer downloads **itself** (curl over HTTPS to a temp name in the
   same directory, verify, then `mv`), never through the libraries'
   downloaders, so no model file goes to `~/.cellpose`, `~/.keras` or
   `~/.deepcell`; `KERAS_HOME=<root>/keras` is exported by the installer
   and the launcher, so Keras' own `keras.json` is written there too:
   * cpsam: `https://huggingface.co/mouseland/cellpose-sam/resolve/main/cpsam`
     (BSD-3-Clause per its model card; SHA-256 matches `models.json`; the
     licence screen also shows Cellpose's own note that Cellpose-SAM was
     trained on CC-BY-NC data, labelled as a separate statement);
   * StarDist: `https://github.com/stardist/stardist-models/releases/download/v0.1/python_2D_versatile_fluo.zip`
     (BSD-3); the zip is verified, its `config.json`, `thresholds.json`,
     `weights_best.h5` extracted and verified;
   * Mesmer: DeepCell 0.12.10's own protocol — POST
     `s3_key=models/MultiplexSegmentation-9.tar.gz` to
     `https://users.deepcell.org/api/getData/` with header
     `X-Api-Key: $DEEPCELL_ACCESS_TOKEN`, download the returned URL, extract
     into a staging directory next to the entry, move the CONTENTS of its
     `MultiplexSegmentation/` (`saved_model.pb`, `keras_metadata.pb`,
     `variables/`) to the entry root, verify against `models.json`, remove
     the staging directory (non-commercial academic licence,
     upstream `ASSET_LICENSE`). No token → the installer prints how to get
     one, or where to put an existing copy, and stops before the self-test.

   For each model: present and matching → kept; present and different →
   refused, never overwritten; missing → licence text + link shown, the
   user types `yes` (`--accept-model-licences` for unattended runs prints
   them and proceeds), then downloaded. `--models DIR` is an input source
   only (read, never written): files found there in the same per-entry
   layout are verified and copied into `<root>/models`. All originals must
   be present and verified before step 6, so the self-test downloads
   nothing.
6. **Package alias, then self-test.** First the checkout's own
   `<repo>/.block01/pkg/block01 -> <repo>` and `<repo>/.block01/run/` are
   created (an existing alias pointing elsewhere is refused, not replaced).
   Then, from `<repo>/.block01/run`, with `KERAS_BACKEND` and the model /
   CUDA variables exported: `PYTHONPATH=<repo>` for `seg_runner.selftest`,
   `PYTHONPATH=<repo>/.block01/pkg` for the `block01` start check: `seg_runner.selftest`; every
   engine must report `state == "ok"`, `exit_code == 0` and a device
   classification (cuda / gpu / cpu as the verification record expects —
   StarDist cpu is fine); then `model_manifest.py --verify` (includes the
   generated Mesmer torch digest); then a background-correction GPU smoke
   check (`core/bg_correction.py:101`) — failure is a warning (CPU
   fallback), not an error; then an offscreen start of `block01` that
   prints which viewer core it accepted (import + adapter check only; not
   a rendering test, see §4).
7. **Install record** for this checkout: `<repo>/.block01/install.env`
   (gitignored) with the env prefix, the model and CUDA variables, the
   checkout path and the core version, written last, then the env's
   `.block01_install_ok` marker. The final line prints the start command.

## 3. Launcher `scripts/block01`

Reads `<its own checkout>/.block01/install.env` (so each checkout runs its
own install; no global link that could point at another checkout).
Refuses clearly when the record is missing, names another checkout, the
link does not point at this checkout, or the env's core fails the adapter
check ("install again into a new location: scripts/install_block01.sh
--root NEW_DIR"). Exports `PYTHONPATH=<repo>/.block01/pkg`,
`KERAS_BACKEND=tensorflow`, the model and CUDA variables, `cd`s into
`<repo>/.block01/run` (CUDA's `cufile.log` lands there), and `exec`s
`<prefix>/bin/python -m block01.main "$@"`. `--desktop` writes a `.desktop`
entry on request.

## 4. README and the AI playbook

* `README.md` Quick start: the two commands; details in
  `envs/fusion_mesmer/README.md` (its Install section points to the
  installer, the manual steps stay as reference); user guides' launch
  sections updated the same.
* NEW `docs/install_with_an_ai_agent.md` (user choice: playbook now): for a
  coding agent the user runs with their own account (Claude Code, Codex):
  run `--check-only`, explain the report, fix only what it lists and only
  with the user's consent (system packages, driver, CUDA toolkit path,
  disk), run the installer, show licences to the user rather than accepting
  them itself, run the launcher; never upgrade pinned packages, never touch
  project data, never store API keys. The in-app "diagnose with AI" button
  is a later, separate plan.

## 5. Scope (closed whitelist)

* NEW `scripts/install_block01.sh`, `scripts/block01`,
  `docs/install_with_an_ai_agent.md`,
  `.github/workflows/fusion_viewer_core_release.yml`,
  `native/fusion_viewer_core/wheels.lock` (after the first release),
  `tests/test_core_api_check.py`, `tests/test_install_scripts.py`
* `native/fusion_viewer_core/`: `Cargo.toml`, `Cargo.lock` (version),
  `src/py/mod.rs` (`CORE_API`), `README.md`
* `envs/fusion_mesmer/models.json` (two `env_override` fields),
  `scripts/model_manifest.py` (the same two in its `MODELS` table),
  `seg_runner/engines.py` (`StardistEngine` loads from
  `BLOCK01_STARDIST_MODEL_PATH` when set), `tests/test_seg_runner.py` (that
  path: same model files → same labels as `from_pretrained`; the
  `from_pretrained` baseline runs with a temporary `HOME`, since it
  extracts its zip into `~/.keras`)
* `ui/step1_core_adapter.py` (the check), `ui/step0/overview_panel.py`
  (fusion uses the check), `utils/closing_threads.py` (shutdown uses the
  check), `ui/step1_viewer_mount.py` (notice shows the reason — if
  authorized)
* `.github/workflows/block01_cpu_tests.yml` (`bash -n` + `shellcheck` on the
  two scripts), `tests/ci_cpu_modules.txt` (the two new test modules)
* `README.md`, `docs/user_guide.md`, `docs/用户指南.md`,
  `envs/fusion_mesmer/README.md`, `.gitignore` (`.block01/`, `cufile.log`)
* this plan

No change to the environment lock, scientific behaviour or project data.

## 6. Tests and acceptance

* Unit (`test_core_api_check.py`): missing module, missing `CORE_API`,
  different `CORE_API`, older / equal / newer version; Step0 engine refusal
  and fusion execution both refuse an incompatible (fake) core; shutdown
  skips `wait_closing`.
* `test_install_scripts.py` (no network, no real env): preflight report
  parsing, prefix state decisions (absent / ours complete / ours
  interrupted / foreign / differing), model state decisions (match /
  missing / mismatch never overwritten), launcher refusals (no record,
  other checkout, wrong link) — the scripts' decision functions are
  testable with `--dry-run` / stubbed commands.
* Release workflow: a `workflow_dispatch` dry run builds and tests both
  wheels without publishing; the first real tag `core-v0.2.0` after
  authorization; `wheels.lock` committed from its `SHA256SUMS`, then checked
  independently by downloading.
* Installer, this machine, fully isolated: run from a **temporary clone of
  the checkout** (`git clone` into the scratch dir, so its `.block01/` is its
  own), temporary `--root`, temporary `HOME`, empty `MAMBA_ROOT_PREFIX` /
  `CARGO_HOME` / `RUSTUP_HOME`, and the model variables unset;
  before/after listings (path, size, mtime, digest) of
  `/root/micromamba/envs/fusion_mesmer`, the existing model roots,
  `~/.cellpose`, `~/.keras`, `~/.deepcell`, `/sda1/Fusion/a9_bench` and the
  real checkout (incl. any `.block01/`) must be identical.
  Cases: fresh run completes (time measured); second run "already
  installed"; `--check-only` writes nothing; interrupted run (killed
  during the env step) → next run recovers; a foreign prefix refused; a
  model mismatch refused; Mesmer without token stops before the self-test;
  `unshare -n` → readable network error. Temporary root deleted after.
* Clean Linux baseline: the installer run in a clean container
  (`ubuntu:22.04` with the host's NVIDIA runtime if available, else the
  CPU-only parts: preflight, env, core, launcher refusal paths), so host
  leftovers cannot hide a missing step.
* Rendering acceptance (not the offscreen start): from the temporary
  install, the launcher starts the app on the real display; a synthetic
  slide opens through Step0 and Step1, the log names the Rust layer
  (`Step1CoreLayer`) and core 0.2.0 / API 1, pixels show, pan / zoom /
  channel switch work. Incompatible core, two separate checks: (1) the
  launcher with an incompatible fake core installed in the test env →
  clear refusal, app not started; (2) the app started directly
  (`python -m block01.main` with the same env, no launcher) → Python
  viewer, the notice shows the reason, pixels show.
* Real-machine acceptance (user): install into a new root on this server
  with `scripts/install_block01.sh`, start with `scripts/block01`, open
  TMA2_test read-only, check the viewer and one segmentation engine.

## 7. Codex plan review round 1 — answers

* P2 model order → §2.5/2.6: originals present and verified before the
  self-test; self-test never downloads; full verification after it.
* P2 model locations → `<root>/models`, all four variables exported and
  recorded, mismatch never overwritten, tests redirect every root.
* P2 whitelist → `src/py/mod.rs`, `Cargo.lock`, core README, CPU workflow,
  CI list, fusion + shutdown callers, notice (for authorization).
* P2 existing env → install root + lock-keyed prefix, start / ok markers,
  foreign prefixes never touched, interrupted runs recovered.
* P2 launcher → per-checkout record and link, mismatch refused,
  `KERAS_BACKEND` exported before Python, `PYTHONPATH=<repo>` for the
  self-test.
* P2 bootstrap trust → pinned micromamba digest, rustup with published
  digest and `--no-modify-path`, maturin pinned, `--locked`, prerequisites
  and all endpoints in preflight; prebuilt wheels verified against the
  committed `wheels.lock`.
* P2 CUDA → resolved the way CuPy does, recorded, GPU smoke check; engines
  and rendering checked separately.
* P2 acceptance → self-test checks `exit_code` and device, rendering on the
  real display with the named Rust layer, refusal paths, before/after
  listings, clean container baseline.
* P3 facts → §0 corrected (adapter lines, workflow artifacts, R0 version,
  content digest).
* §4 → user chose prebuilt wheels (1.2), licence-then-download (2.5), and
  the playbook now (4).

## 8. Codex plan review round 2 — answers

* P2 model paths → §2.5: one contract, every entry has an `env_override`
  (`CELLPOSE_LOCAL_MODELS_PATH`, NEW `BLOCK01_STARDIST_MODEL_PATH` with a
  folder load in `StardistEngine`), the installer downloads itself (no
  library downloader, no HOME caches), `--models` read-only input; files
  added to §5.
* P2 smoke before alias → alias created at the start of §2.6; the start
  check uses `<repo>/.block01/pkg`.
* P2 record isolation → acceptance runs from a temporary clone; the real
  checkout and `~/.deepcell` in the before/after listings.
* P2 launcher vs fallback → split into launcher refusal and direct start.
* P3 → `started` marker only for a prefix being created; remedy "--root
  NEW_DIR"; Hugging Face + DeepCell in preflight; tag rule `core-v`
  stripped, dry run never publishes; manylinux tag and `-i python3.10`
  explicit.

## 9. Codex plan review round 3 — answers

* P2 `--write` → `scripts/model_manifest.py` `MODELS` gains the two
  `env_override` values; added to §5.
* P2 marker timing → ownership is a sibling `.owner` file written before
  `micromamba create`, so an interruption at any point is recognized; an
  unowned prefix is never touched.
* P2 DeepCell layout → staged extraction, contents of
  `MultiplexSegmentation/` moved to the entry root, then verified.
* P3 `~/.keras` → `KERAS_HOME=<root>/keras` exported by installer and
  launcher; the StarDist baseline test uses a temporary HOME.
* Round 4: READY; its P3 (show Cellpose's training-data note next to the
  model-card licence) applied in §2.5.

## 10. Implementation notes (authorized 2026-11-01: plan, release workflow, notice line, StarDist folder load)

* Core: `CORE_API = 1` (`src/py/mod.rs`), version 0.2.0 (`Cargo.toml`,
  `Cargo.lock`); `core_unavailable_reason()` checks module / `CORE_API` /
  `MIN_CORE_VERSION`; fusion (`overview_panel._build_fuse_job`) and the exit
  wait (`closing_threads.wait_all`) ask it; the Step1 notice adds "The fast
  viewer is not used: <reason>." The server's `fusion_mesmer` got the 0.2.0
  wheel (otherwise the new check would refuse its 0.1.0 core).
* **Deviation, confirmed by the user (TG 2026-11-01):** a newer core for an
  environment the installer created (owner file + markers) replaces only the
  core wheel, instead of "install into a new --root" (12 GB). Environments
  it did not create are still never touched. The refusal hint is therefore
  "run scripts/install_block01.sh".
* Installer additions found necessary in testing: an env marker
  `.block01_env_ok` after `pip check`, so a run stopped at a later step keeps
  the environment (state `built`) instead of rebuilding it; no `cmd | head`
  under `pipefail` (a SIGPIPE made glibc read as "2.39\n0" now and then);
  the self-test check is its own function (`check_selftest`); stale
  `*.part.*` downloads removed; without curl the network check says "not
  checked".
* Models: `env_override` for cpsam (`CELLPOSE_LOCAL_MODELS_PATH`) and StarDist
  (`BLOCK01_STARDIST_MODEL_PATH`, folder load in `StardistEngine`);
  `model_manifest.py --write` reproduces `models.json` byte for byte.
* Release workflow: manylinux_2_28 Linux job reproduced locally in
  `quay.io/pypa/manylinux_2_28_x86_64`: wheel built `--locked`, installed in
  a clean venv, 0.2.0 / API 1, `test_v16_5_r1_ci.py` 41 passed. Not pushed,
  no tag: the user decides on publishing after comparing Odon and Block01.
* Tests: `test_core_api_check.py` 15, `test_install_scripts.py` 17,
  `test_seg_runner.py` + 1 (StarDist folder = `from_pretrained`, temporary
  HOME); CI list + 2; CI workflow runs `bash -n` + shellcheck 0.11.
  Targeted regression (adapter / viewers / Step0 engine / Step3 / fusion /
  seg_runner, real display): 578 passed.
* Isolated install on this server (temporary copy of the checkout, root,
  HOME; `env -i`; models: cpsam + StarDist downloaded, Mesmer via
  `--models`): killed during `micromamba create` → next run removed and
  redid it; full run with `--build-core` 12 min cold / 4 min warm; second
  run 28 s ("already installed"); prebuilt path through a local server
  standing in for the release: downloaded, verified, installed; a tampered
  wheel refused with nothing left behind; `unshare -n` → stops at the first
  download with the URL and what to do. Self-test: cellpose cuda:0, stardist
  cpu, mesmer gpu, GPU background correction ok, app start check accepts
  core 0.2.0 / API 1. Before/after listings of `fusion_mesmer`, the model
  roots, `~/.cellpose`, `~/.keras`, `~/.deepcell`, `a9_bench` and the real
  checkout: every digest identical (three StarDist mtimes in `~/.keras`
  changed at 14:30:42, during the regression run — the existing
  `from_pretrained` test re-extracts its zip — before any install run).
* Rendering with the installed environment (real display, the clone's
  package): Step0 engine live, Step1 tile, Step1 mount, display TIFF, API
  check — 82 passed in 3 of 4 runs; the first (slower, 112 s vs 63 s, cold
  caches of the new environment) had one failure whose name was not kept.
  `scripts/block01` started the app on the real display (screenshot: Step 0
  window).
* Clean container (`ubuntu:22.04`, `--gpus all`, the release wheel served
  locally, models via `--models`): found two things this server hides —
  `deepcell-toolbox` has no wheel and is compiled by pip (a C compiler is
  now a preflight requirement, with the install command), and pip's Qt 5
  needs 21 system libraries a bare system lacks (`check_system_libs`: names
  the missing files and the one `apt install` line). The start check now
  shows its error output (`startcheck.err`) instead of "no answer". With
  `curl bzip2 build-essential` + the printed library line installed: env
  built, prebuilt core downloaded, engines ok (cellpose cuda:0, stardist cpu,
  mesmer gpu), GPU background correction → CPU warning (no CUDA toolkit in
  the container: CuPy needs libnvrtc), start check accepted, `scripts/block01`
  ran the app offscreen; the rerun after the library install continued from
  the `built` environment in 25 s. Optional Qt modules (multimedia, Wayland)
  still lack their libraries; Block01 imports none of them.
* Codex code review round 1: FAIL, 4 P2 + 2 P3, all applied: the release
  job publishes only on a pushed tag (`github.event_name == 'push'`); pip,
  CuPy, CUDA JIT and matplotlib caches under `<root>/cache`, micromamba run
  with its own HOME and `CONDA_PKGS_DIRS` (it added the prefix to
  `~/.conda/environments.txt` and made `~/.mamba`); model import copies only
  the manifest's files, dereferenced (`cp -L`), each moved to a path that did
  not exist, so foreign files in the destination stay; the space check counts
  only the work left; one install per root (`flock` on `<root>/.install.lock`,
  `flock` in preflight). Verified: a complete rebuild of the environment with
  an EMPTY temporary HOME (534 s, warm package cache) left HOME empty; the
  caches are in `<root>/cache/{pip,cupy,nv,matplotlib}`. Tests +6
  (`test_install_scripts.py` 23).
* Codex code review round 2: FAIL, 1 P2 + 1 P3, both applied: an owned
  interrupted environment's size (`du`) counts as free in the space check
  (it is removed first); every StarDist test in `test_seg_runner.py` runs
  with a temporary HOME holding a copy of the deployed zip (the
  `stardist_ok` fixture), so the module no longer re-extracts into the real
  `~/.keras` (checked: mtime unchanged after the module, 15 passed).
* Codex code review round 3: PASS, no findings. Not committed: the user decides on commit / push / the first release after comparing Odon and Block01 (TG 2026-11-01).
