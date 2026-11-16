# A one-command Mac install (backlog 23) — plan v0.2

Status: the user started the block ("先做mac安装器", 2026-11-16) and took
every recommendation of §6 ("同意"): M1 Apple silicon, macOS 13+; M2 a
separate installer (the launcher `scripts/block01` is shared: it names the
Mac installer on a Mac); M3 the prebuilt macOS arm64 viewer core added to
release `core-v0.2.1` and the DeepCell-Toolbox wheel committed in
`envs/macos/wheels/`, both built by CI; M4 `~/Applications/FusionFlux.app`;
M5 this Mac mini.

v0.2 (2026-11-16), what was done and found:
* `scripts/install_block01_mac.sh` written (bash 3.2, BSD tools) and run
  end to end on this Mac (macOS 27.2, Apple M6) into a scratch root with
  locally built wheels standing in for the CI ones: fresh install 4 min 5 s
  (environment 1 min 52 s), the self-test ok for Cellpose, StarDist and
  Mesmer on the GPU (MPS) and on the CPU, MPS background correction ok, the
  app start check ok; a rerun 20 s, everything "already installed";
  `--mesmer-only` ok.
* The default root is `~/.local/share/block01` (as on Linux), not
  `~/Library/Application Support/block01`: conda environments must not live
  under a path with a space; step 1 refuses such a `--root`.
* Codex review: two real findings, fixed -- the install lock (a folder with
  its owner's pid; no `flock` on a Mac) could be taken while its owner had
  not written its pid yet, and `--mesmer-only` took no lock. A test then
  found a third: `kill -0` fails for another user's process, so such a
  running install looked stale; `ps -p` is used.
* DeepCell's official Mesmer archive: its bucket now answers `301
  PermanentRedirect` naming `s3-us-west-2`, which answers 403 -- the archive
  is no longer public. curl saves the redirect's XML (exit 0), so the
  installers say "not the expected file" and go on to the lab's mirror, as
  designed (seen on the Windows laptop twice and here).

## 1. Goal and target

`bash scripts/install_block01_mac.sh` on a Mac installs FusionFlux the way
`install_block01.ps1` does on Windows and `install_block01.sh` on Linux:
one command, nothing it did not create changed, every download pinned and
checked, a rerun continues, the same terminal output (one line per step
updated in place, the blinking bullet, one licence card, the self-test
table, the tools' output in `.block01/logs/install-<time>.log`), and a way
to start the app by double-click.

Proposed target (§6 M1): **Apple silicon (arm64), macOS 13 or newer**,
Python 3.10; the engines on the Mac GPU (PyTorch MPS), CPU fallback.
Not targeted: Intel Macs (PyTorch has no macOS x86_64 wheel after 2.2;
the pinned 2.5.1 has none).

## 2. Today (audited 2026-11-16, HEAD 9fc9129)

* The Mac already runs the whole pipeline (the Mac port, 2026-11-08 to
  11-10): `work/envs/block01_mac` -- Python 3.10.22 arm64, torch 2.5.1
  (MPS), cellpose 4.1.1, stardist 0.9.2 / csbdeep 0.8.2, tensorflow 2.15.1 /
  keras 2.15.0 (StarDist's model shell only), DeepCell 0.12.10 and
  DeepCell-Toolbox 0.12.1 (`--no-deps`), fusion_viewer_core 0.2.1. It was
  made by hand; `envs/windows/requirements*.txt` say they are "the Mac's
  engine set" -- the Mac install reuses them minus `cupy-cuda12x`.
* Step0's GPU background correction on the Mac uses MPS
  (`core/mps_kernels.py`), no CuPy.
* The engines pick `mps` when there is no CUDA (`seg_runner/engines.py:210`).
* Started by `work/envs/run_block01_mac.sh` with hand-set model paths.
* **No macOS viewer core wheel**: release `core-v0.2.1` has the Linux and
  Windows wheels only; `native/fusion_viewer_core/wheels.lock` names no
  macOS entry. The Mac's core was built here with Rust.
* **DeepCell-Toolbox** has a compiled part (`compute_overlap`); PyPI has
  only its source, so a Mac without Xcode's command-line tools cannot
  build it (the Windows answer: a wheel shipped in the checkout, W3-D5).

## 3. Work (one block, closed file list)

New:
* `scripts/install_block01_mac.sh` -- the Mac installer (bash 3.2, the
  Mac's own bash; BSD tools: `shasum -a 256`, `stat -f`, no `flock`/`setsid`).
  Steps: computer check (arm64, macOS version, free space, MPS) -> licence
  card -> micromamba (osx-arm64, pinned release and digest) -> environment
  (conda-forge only; `requirements-cpu.txt`, torch/torchvision 2.5.1 from
  PyPI (MPS builds), `envs/windows/requirements.txt` without cupy,
  DeepCell-Toolbox from the shipped wheel, `requirements-nodeps.txt`) ->
  viewer core (prebuilt wheel, digest from `wheels.lock`) -> models (the
  same sources and checks; Mesmer official -> mirror -> archive -> folder)
  -> StarDist torch conversion -> package link -> self-test (MPS and CPU
  columns) + MPS background correction + app start check -> record ->
  the app (§6 M4).
* `scripts/block01_mac` (or a Darwin branch of `scripts/block01`, §6 M2):
  the launcher -- the record checks of the Linux one.
* `envs/macos/wheels/DeepCell_Toolbox-0.12.1-cp310-cp310-macosx_*_arm64.whl`
  + `envs/macos/wheels.lock` (built by the toolbox workflow, §6 M3).
* `tests/test_install_scripts_mac.py` (static checks, as for Windows).

Changed:
* `.github/workflows/deepcell_toolbox_wheel.yml` -- a `macos-14` job.
* `.github/workflows/fusion_viewer_core_release.yml` -- a `macos-14` arm64
  wheel next to the Linux and Windows ones (§6 M3).
* `native/fusion_viewer_core/wheels.lock` -- its `0.2.1 macos` line.
* `.github/workflows/` -- a `mac-install` CI job: the installer with
  `--skip-models` on `macos-14`, a rerun, the launcher's refusal.
* README / `docs/install_with_an_ai_agent.md` -- a Mac section.

Not changed: `install_block01.sh` (Linux, accepted), the .ps1, product code.

## 4. Acceptance

1. CI `mac-install` green (install from scratch, rerun, launcher refusal).
2. On this Mac mini: a from-scratch install into a new root (the real
   project data are not touched), double-click start, Step0-4 on the
   synthetic or demo data; the user looks at the terminal output and the
   app.

## 5. Risks and rollback

* MPS in the self-test: GitHub's macOS runners have no usable MPS; CI checks
  the CPU column only, the Mac mini the MPS one.
* Gatekeeper: files we write locally carry no quarantine flag, so a
  locally made `.app` opens without a warning; a downloaded one would not.
* Rollback: the new files only; the hand-made Mac environment is not used
  or changed by the installer.

## 6. Decisions for the user

* **M1 target**: Apple silicon, macOS 13+ (recommended); Intel Macs not
  supported.
* **M2 script**: a separate `install_block01_mac.sh` (recommended: the
  Linux one is accepted and full of Linux-only checks; a separate script
  keeps it untouched) -- or one shared script with Darwin branches.
* **M3 prebuilt parts**: the macOS arm64 viewer core wheel published as a
  new asset of the existing release `core-v0.2.1` (an outward action on the
  public repository) and its digest in `wheels.lock`; the DeepCell-Toolbox
  macOS wheel committed in `envs/macos/wheels/` like the Windows one
  (recommended) -- else users need Rust and Xcode's tools.
* **M4 double-click**: a small `FusionFlux.app` in `~/Applications`
  (recommended; opens without a Terminal window, shows in Launchpad /
  Spotlight) -- or a `FusionFlux.command` on the Desktop (opens Terminal).
* **M5 acceptance machine**: this Mac mini, into a fresh install root.

v0.2 continued (2026-11-16, the user's "同意" to commit, build and publish):
* GitHub no longer serves `macos-14` ("macos-14-arm64-us-default is not
  supported"): the Mac jobs run on `macos-latest` (Apple silicon); the
  wheels still target macOS 13.
* The CI wheels: viewer core `fusion_viewer_core-0.2.1-cp310-abi3-macosx_13_0_arm64.whl`
  (run 43804575267, sha256 9c3d93af...) added to release `core-v0.2.1` with
  `SHA256SUMS` now listing three wheels; DeepCell-Toolbox
  `DeepCell_Toolbox-0.12.1-cp310-cp310-macosx_13_0_universal2.whl`
  (run 43804571743, sha256 97ce2969...; universal2 holds arm64) in
  `envs/macos/wheels/`.
* A fresh install on this Mac from the public release and the CI toolbox
  wheel (`--models` from the first test): 3 min 0 s, the self-test ok on the
  GPU and the CPU, the app start check ok.
