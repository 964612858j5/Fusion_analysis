# Installing Block01 with a coding agent

For a coding agent (Claude Code, Codex CLI, …) that a user runs on their own
Linux machine with their own account, asked to "install Block01 following
docs/install_with_an_ai_agent.md". The installer does the work; the agent
reads its reports, explains them, and fixes what they list — with the
user's consent. (docs/v17_install_plan.md §4.)

## Rules

1. Ask the user before every command that changes the system (package
   manager, driver, CUDA toolkit, disk clean-up, environment variables in
   shell start files). Read-only commands need no question.
2. Never accept a model licence for the user. When the installer shows a
   licence, show it to the user and let them type `yes` themselves, or run
   with `--accept-model-licences` only after the user has said in their own
   words that they accept every licence shown.
3. Never edit the pinned files (`envs/fusion_mesmer/*`, `Cargo.lock`,
   `wheels.lock`, `models.json`) and never upgrade or add packages inside the
   Block01 environment. If the installer says the pinned packages do not fit
   this machine, stop and report — do not work around it.
4. Never touch user data: image files, Block01 project folders, anything
   outside the checkout and the install root.
5. Never store API keys or tokens in files. (The installer needs none: the
   Mesmer model comes from DeepCell's public model storage.)
6. Do not modify an existing environment, model folder or link the installer
   refused to touch; ask the user what to do with it, or use a new `--root`.

## Steps

1. From the checkout: `bash scripts/install_block01.sh --check-only`
   (writes nothing). Explain each line to the user. Lines marked NOT
   SUPPORTED / MISSING / NOT ENOUGH must be fixed first; WARNING lines are
   optional (they say what runs slower without the fix).
2. Fix what step 1 listed, one item at a time, with the user's consent:
   * missing tools (`curl`, `tar`, `bzip2`, a C compiler): the distribution's
     package manager (Ubuntu/Debian: `build-essential` for the compiler);
   * no NVIDIA driver or an old one: tell the user; installing a driver needs
     their decision and usually a reboot;
   * no CUDA toolkit: GPU background correction falls back to the CPU; the
     verified toolkit is CUDA 12.2 (point `CUDA_PATH` at it for the run);
   * not enough space: ask the user where to install (`--root DIR`).
3. Mesmer is downloaded from DeepCell's public model storage
   (`deepcell-data.s3-us-west-1.amazonaws.com`), no account needed; its
   licence (non-commercial academic use) is shown and must be accepted like
   the others. An existing copy can be used instead with `--models FOLDER`
   (`FOLDER/<models.json entry>/…`).
4. Run `bash scripts/install_block01.sh` (with `--root`, `--models` as
   agreed), in a terminal so the licences can be answered. It also builds a
   second small environment for Step 4's SpatialData export (about 1.2 GB);
   add `--no-spatialdata` only if the user says they do not need it.
5. If it stops: it prints `STOPPED at: <step>`, the failing command and
   `what to do`. Do that (rules above), then run the same command again —
   finished steps are skipped, a half-finished environment is redone.
6. At the end it prints the start command. Run `scripts/block01` and confirm
   with the user that the window opens. If it says *cannot start*, it names
   the reason and the fix.

## What a good report to the user contains

What was checked, what was changed on their machine (with their consent),
which licences they accepted, where everything was installed, the start
command, and anything left over (warnings, a slower CPU fallback).
