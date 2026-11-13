# A native Windows version (backlog 23) — plan v0.3

Status: draft for the user (2026-11-13). Codex plan review round 1 FAIL
(one P1: the SpatialData export script; four P2: file aliases, closed file
lists, W1 needing W2's environment, the GL impact) -- all taken in v0.2.
Round 2 FAIL (P1: ending a process tree on Windows; P2: W4's file list,
the SpatialData prerequisite, the old checklist's §4) -- taken in v0.3.
Round 3 PASS.

**The user's decisions (2026-11-13):** W0 started. D1 as proposed
(Windows 10 / 11, x64, NVIDIA CUDA 12, CPU fallback). D2 micromamba.
D3 the lab's Windows laptop with an NVIDIA GPU. D4 later. D5 the script
install (W3) is enough: **W4 is not planned**. Nothing here is started; every
phase is its own block with a closed file list, started by the user. It
builds on `docs/v17_acceptance_notes_plan.md` B23 (the first readiness
note), `docs/v17_windows_acceptance.md` (the checklist and the gaps of the
PySide6 build) and the Mac port (2026-11-08 to 11-10), which already moved
every inference to PyTorch and is the model for the Windows environment.

## 1. Goal and target

FusionFlux (Block01) installed, started and run on **native Windows**
(not WSL): the whole pipeline -- Step0 correction, Regions, Step1 fusion,
Step2 segmentation (Cellpose, StarDist, Mesmer, the hybrids), Step3 QC,
Step4 features (h5ad; SpatialData through Step5's environment) -- with
the same outputs as on Linux and the Mac.

Proposed target (to be confirmed, §7): Windows 10 22H2 / Windows 11, x64,
an NVIDIA GPU with a CUDA 12 driver (CPU-only still works, slower);
Python 3.10, as on Linux and the Mac. Not targeted: ARM Windows, AMD/Intel
GPUs for inference (Torch's DirectML is not a supported route here).

## 2. Today (audited 2026-11-13, HEAD 0fec6ef)

### 2.1 Code that breaks on Windows (blockers)

| where | what | effect on Windows |
|---|---|---|
| `seg_runner/client.py:69` | `Popen(..., start_new_session=True)` | POSIX only (no process group) |
| `seg_runner/client.py:83-85` | `os.killpg(pid, SIGTERM/SIGKILL)` | `os.killpg` does not exist: Stop and every engine end fail |
| `seg_runner/client.py:17, 187-188` | `selectors` on the engine's stdout pipe | Windows `select` takes sockets only: **no engine reply is ever read** -- every Step2 / Step1 pre-segmentation run fails |
| `workers/segment_merge_worker.py:1439` | `os.killpg` on Stop while the engine loads | Stop fails |
| `seg_runner/mesmer_torch/prepare.py:17, 82-86` | `import fcntl` at module top, `flock` | the module does not import: Mesmer cannot be prepared |
| `ui/step0/gl_host.py:95` | `ctypes.CDLL(find_library("GL") or "libGL.so.1")` | no such library; the error is caught (`:109`) and only the **gaps between Step0's compare panels** stay unpainted (`:1192-1194`, `_fill_gaps`); the picture itself is blitted (`:1181`). On Windows the library is `opengl32.dll` |
| `scripts/export_spatialdata.py:20` | `import resource` at module top | Step4's SpatialData export (launched by `workers/feature_extract_worker.py:341`) fails on Windows even with a working SpatialData environment |

### 2.2 Soft (a fallback exists or the effect is small)

* `core/bg_parallel.py:52` `/proc/meminfo` -> psutil (done for the Mac).
* `workers/feature_extract_worker.py:99` `resource.getrusage` -> needs
  psutil on Windows (peak RSS for a log line).
* `seg_runner/selftest.py:27` `/proc/<pid>/status` -> psutil.
* `workers/segment_merge_worker.py:1401` `os.symlink` for the run's
  aliases (`_make_alias`): on Windows it needs Developer Mode or admin
  rights, and the fallback **copies the whole source**. The aliases are
  folders (`global_mask.zarr`, `:1416`) **and files** (the OME-TIFFs,
  `:1414-1415`, `:3762-3763`). Windows needs a cheap alias for both: a
  directory junction for a folder (no rights needed, same volume), a hard
  link (`os.link`, NTFS, same volume, no rights needed) for a file; a copy
  only when neither is possible, and then logged.
* `nvidia-smi` (`ui/step2_page.py:2197`, `segment_merge_worker.py:1607`):
  shipped with the Windows driver as `nvidia-smi.exe`; works.
* cuCIM: Linux only, but background correction already runs on CuPy
  (`core/bg_correction.py:163`); cuCIM is only probed.

### 2.3 Windows file semantics (not visible in a grep; the larger risk)

Windows refuses to rename, replace or delete a file or folder another
handle has open; Linux and macOS allow it. Places that do so:

* `core/product_swap.py:108-111, 148, 219-220`: Step0's in-place Save
  renames folders of the corrected product while viewers may still read
  it (B18 keeps the picture during the hand-off).
* Run deletion / moves (`utils/run_store.py`, the rm2 chain) while a
  viewer has the run open.
* Memmaps removed after a run (`segment_merge_worker.py:3686, 4648`):
  every `np.memmap` must be closed (deleted) before `os.remove`.
* 28 `os.replace`, 7 `os.rename` / `shutil.move`, 32 `shutil.rmtree`,
  21 removes in 20+ files: each is fine for a file nobody holds, a
  `PermissionError` otherwise.
* Paths: drive letters (`os.path.relpath` across drives raises
  `ValueError` -- project manifests store relative paths); the 260
  character limit (deep run folders); case-insensitive names.

### 2.4 Dependencies

| package | Windows | note |
|---|---|---|
| PySide6 6.11.3, pyqtgraph, PyOpenGL, numpy, scipy, scikit-image, zarr 2, tifffile, imagecodecs, numba, psutil, h5py | wheels | |
| fusion_viewer_core 0.2.x | `win_amd64` wheel built by `fusion_viewer_core_release.yml` | |
| torch 2.5 + CUDA 12.x | wheels (`download.pytorch.org`) | all inference |
| cellpose 4.1.1 | pip | |
| stardist 0.9.2 + csbdeep | pip | needs TensorFlow for its `StarDist2D` shell only (the network runs in Torch, `seg_runner/engines.py:290-330`) |
| tensorflow (CPU) | Windows CPU wheels up to 2.15 (`tensorflow-intel`) | as on the Mac (2.15.1); no GPU needed |
| deepcell 0.12.10 / deepcell-toolbox 0.12.1 | installed `--no-deps` on the Mac; deepcell-toolbox has compiled parts: **a Windows wheel must be checked** (risk R2) | Mesmer post-processing |
| cupy-cuda12x | wheels | Step0 GPU correction |
| cuCIM | **none** | not needed (§2.2) |
| SpatialData export (Step5's env, `utils/spatialdata_python.py`) | spatialdata 0.5 has wheels; Step5's env on Windows is Step5's own work | |

The models (Mesmer weights: DeepCell non-commercial, never in the repo;
StarDist; Cellpose) are fetched and converted on the user's machine with
the existing scripts (`scripts/convert_mesmer_to_torch.py`,
`scripts/convert_stardist_to_torch.py`, `prepare.ensure`), as on the Mac.

### 2.5 Install and start today

Bash only: `scripts/install_block01.sh` (783 lines), `scripts/block01`
(launcher: `check_install`, env, `exec python`). No `.ps1` / `.cmd`, no
Windows CI job for the application (only the viewer-core wheel is built on
`windows-latest`).

## 3. Phases (each one block, started by the user)

Order: W0 (discovery, read-only on the product) -> W1 (portability) ->
W1b (file handles, its file list fixed by W0) -> W2 (engines) -> W3
(installer) -> W4 (optional packaging). No block's file list is open:
where it cannot be known before a Windows run (W1b), a discovery step
produces it and the user approves it before the block starts.

### Phase W0 — discovery and feasibility (no product code changed)

* `.github/workflows/block01_windows_tests.yml` (new): `windows-latest`,
  Python 3.10, the candidate environment, the CPU test suite offscreen
  (`QT_QPA_PLATFORM=offscreen`), one test per process, never failing the
  push (a report, not a gate, until W1b).
* `envs/windows/requirements.txt` and `envs/windows/requirements-nodeps.txt`
  (new, candidate): the Mac's set (§2.4) for Windows, DeepCell and
  deepcell-toolbox in the second file (installed `--no-deps`, as on the
  Mac); the job installs them part by part and imports every package
  (deepcell-toolbox's compiled part, `compute_overlap`: R2) and the
  viewer-core wheel built from the checkout.
* Output, to the user: the list of tests failing on Windows grouped by
  cause, each POSIX call or `PermissionError` with its file:line, and
  whether the environment installs. From it, W1b's exact file list.

### Phase W1 — portability (closed file list)

* `seg_runner/proc.py` (new): start an engine in its own process group
  (POSIX `start_new_session`; Windows `CREATE_NEW_PROCESS_GROUP`) and end
  its process tree (POSIX `killpg` SIGTERM / SIGKILL as today). On
  Windows the **tree first, its root last**: the children are listed
  (psutil) before anything is ended, then `taskkill /PID <root> /T` (a
  polite end of the whole tree), after the grace time `taskkill /T /F`,
  and the listed children are waited for -- never `terminate()` on the
  root first (it would orphan a child such as Mesmer's conversion,
  `seg_runner/mesmer_torch/prepare.py:126`, which `/T` then no longer
  finds). A child still alive is reported.
* `seg_runner/client.py`: uses it; the engine's replies read without
  `selectors` on Windows (a reader thread and a queue, `_read`'s timeout
  kept; POSIX unchanged).
* `workers/segment_merge_worker.py`: `:1439` through the helper; the
  aliases (`_make_alias`, `:1393-1406`): a junction for a folder, a hard
  link for a file, a logged copy only as the last resort.
* `seg_runner/mesmer_torch/prepare.py`: the lock with `msvcrt.locking`
  on Windows (`fcntl` imported only on POSIX).
* `ui/step0/gl_host.py`: `_libgl` loads `opengl32.dll` on Windows.
* `workers/feature_extract_worker.py`, `scripts/export_spatialdata.py`,
  `seg_runner/selftest.py`: psutil where `resource` / `/proc` are missing.
* Paths: `core/provenance.py`, `utils/run_store.py`,
  `utils/roi_project.py`, `utils/trash.py`, `core/label_pyramid.py`
  (`relpath` across drives: an absolute path kept instead of a
  `ValueError`), as far as W0 shows them failing.
* Tests (new): `tests/test_seg_runner_proc.py` (start, reply, stop, a
  kill while the model loads -- with a tiny fake engine),
  a stop that leaves no child of a child alive),
  `tests/test_mesmer_prepare_lock.py`, `tests/test_run_alias.py` (folder
  and file aliases, no copy of a large source); they run on Linux, the
  Mac and the W0 Windows job.
* `docs/v17_windows_acceptance.md`: its checks brought in line with
  today's rules (`UI_SURFACE_RULES.md`) before they gate anything --
  4.2 (Step1's ticks now choose what is fused, not only what is shown)
  and 4.11 (Regions keeps its numbering after a delete) -- and the
  Windows items of this plan added (compare-panel gaps, Stop of an
  engine with a child, folder and file aliases).
* Acceptance on the Windows machine, with W0's environment installed by
  hand: the app starts and closes; the updated checklist §3 (100 % and
  150 %) and §4; Step0's compare panels with their gaps painted; one
  Step2 run with Cellpose (GPU and "Use GPU" off) including Stop during
  the run; one Step4 run (h5ad). `scripts/export_spatialdata.py` is
  checked to import and read its job on Windows (a unit test); the
  SpatialData export end to end is accepted once Step5's environment
  exists on Windows (its own work, `utils/spatialdata_python.py`) --
  not a W1 gate. The other methods are W2's acceptance.

### Phase W1b — file handles (file list from W0)

Windows refuses to rename / replace / delete what is open (§2.3). Each
place W0 finds, plus `core/product_swap.py` (Step0's in-place Save,
checked first whatever W0 shows), either needs nobody to hold it or
releases its readers (the viewers' sources, memmaps) before; where a
release cannot be guaranteed, a short retry and then a clear message --
never a half-done swap. The exact files and changes are proposed to the
user from W0's report before this block starts. Acceptance: Step0 Save
in place with the image open, deleting / moving runs that are on screen,
a Step2 run's clean-up, on the Windows machine.

### Phase W2 — engines and GPU

* `envs/windows/requirements.txt`: W0's candidate pinned (torch CUDA 12
  wheels, `tensorflow` CPU 2.15, `cupy-cuda12x`, deepcell `--no-deps`)
  and tested on the machine; `scripts/convert_mesmer_to_torch.py`,
  `scripts/convert_stardist_to_torch.py` only if they fail on Windows.
* Model preparation on Windows: the conversion scripts and
  `prepare.ensure` (paths, locks) run; `seg_runner/selftest` passes for
  each engine on the GPU and with "Use GPU" off.
* Acceptance: one run per method (Cellpose, StarDist, Mesmer, the
  hybrids, pre-segmentation in Step1) on the GPU and with "Use GPU" off.
* TensorFlow: kept (CPU) in this phase. Removing it (StarDist's own pre /
  post without the Keras shell; Mesmer's weights read with h5py) is a
  separate option (§7 D4), useful on every system.

### Phase W3 — installer, launcher, Windows CI

* `scripts/install_block01.ps1` (new): the equivalent of
  `install_block01.sh` -- environment (micromamba, as on Linux / the Mac,
  or a venv; §7 D2), the requirements, the viewer core wheel,
  `.block01\install.env`, the package link as a `.pth` file (no symlink
  rights needed), the models' locations.
* `scripts/block01.cmd` / `block01.ps1` (new): the launcher
  (`check_install`, the GPU check, the env variables of
  `run_block01_mac.sh`, logs in `%USERPROFILE%\.block01`).
* README / `docs/install_with_an_ai_agent.md`: the Windows section.
* CI: `.github/workflows/block01_windows_tests.yml` (W0's) also
  installs from scratch with the new installer and starts the app
  offscreen (a smoke run); from here its test run gates the push.
* Files: `scripts/install_block01.ps1`, `scripts/block01.cmd`,
  `scripts/block01.ps1` (new), `README.md`,
  `docs/install_with_an_ai_agent.md`, the workflow,
  `tests/test_install_scripts.py` (the Windows scripts' static checks).

### Phase W4 — a packaged application (optional, §7 D5)

A double-click installer: PyInstaller or Briefcase (an `.exe` with the
environment inside, or an MSIX), the icon (B22, shelved), an
`AppUserModelID` so the taskbar shows FusionFlux, Start-menu shortcut,
uninstall. Code signing decides whether SmartScreen warns. The models
stay downloaded on first use (licences). Its exact files (expected
under a new `packaging/windows/`, the icon of B22 in `assets/`, `main.py`
for `SetCurrentProcessExplicitAppUserModelID`, one release workflow) are
proposed to the user with the packaging tool chosen (D5) and approved
before W4 starts, as W1b's are.

## 4. Order and effort (rough)

W0 is small (a workflow and a requirements file); W1 is mechanical;
W1b is the bulk (the file handles); W2 is mostly environment work
(the Mac's experience applies); W3 is mechanical; W4 is packaging. Each
needs a Windows machine for its acceptance; the CI job of W1 reduces the
machine time needed.

## 5. Risks and rollback

* R1 Windows file locking (§2.3): the largest; found fully only on a
  Windows machine. Mitigation: the CI job, the swap first, clear messages.
* R2 deepcell-toolbox on Windows: if it has no wheel and does not build,
  Mesmer's post-processing needs its pieces vendored or replaced (a block
  of its own).
* R3 GPU drivers / CUDA versions on the user's machines: the launcher's
  GPU check and "Use GPU" off as the fallback.
* R4 OpenGL on Windows: Qt's desktop GL needs a real driver; over Remote
  Desktop Windows offers GL 1.1 only (the viewers then fall back or
  fail): acceptance on the machine itself, not over RDP.
* Rollback: every change is behind `sys.platform` or a helper with the
  POSIX path unchanged; reverting a phase's commits restores today.

## 6. Tests

Synthetic only. Per phase: unit tests for the new helpers on every
system; the Windows CI job; the existing suite unchanged on Linux / the
Mac (one test per process on the Mac). Real-machine acceptance on Windows
is the gate of every phase.

## 7. Decisions for the user

* D1 Target: Windows 10 + 11, x64, NVIDIA (CUDA 12) with CPU fallback --
  or narrower / wider?
* D2 Environment: micromamba (as on Linux / the Mac; one tool everywhere)
  or a plain venv + pip (fewer moving parts on Windows)?
* D3 A Windows machine with an NVIDIA GPU for acceptance (remote access,
  not Remote Desktop for the GL checks) -- which, and from when? Until
  then W1 can start with the Windows CI job.
* D4 Remove TensorFlow (StarDist shell, Mesmer conversion) now or later?
* D5 Is a packaged installer (W4) needed, or is the source install (W3)
  enough for now?
