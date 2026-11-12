# v17 — PySide6 wrap-up (M4 and the open items of docs/v17_pyside6_plan.md §9)

Status: plan v1.3, 2026-11-08 (v1.1 added item G; v1.2 takes codex round 1; round 2 PASS, v1.3 its wording advisories — §8). **Authorized by the user 2026-11-08 (TG), implemented — §10.** Original header: read-only investigation on the Mac mini (the
Linux A9 server is down). Requested by the user (TG 2026-11-08: "继续做
pyside6收尾"), review by codex astra **high** (one-off exception granted by
the user; the default stays low). **Not authorized for implementation** —
the user approves after the review.

HEAD b22cdcd, branch v17. The working tree also holds the uncommitted Step2
skip-tiles block (docs/v17_step2_skip_tiles_ux_plan.md, awaiting acceptance);
this block touches none of its files and is committed separately by path.

## 1. What is left (measured)

| # | Item | Evidence | Public path? |
|---|---|---|---|
| A | **The installer's pip list still installs PyQt5 and no PySide6.** | `scripts/install_block01.sh:305` installs `envs/fusion_mesmer/requirements-pip.txt` (`--no-deps`). That file pins `pyqt5==5.15.11`, `pyqt5-qt5==5.15.18`, `pyqt5-sip==12.18.0` (lines 159–161) and contains no `pyside6*`/`shiboken6` (grep: 0). RT2 (f24495d) updated `environment.yml:179-180` and `envs/ci/requirements-cpu.txt:37-38` but not this file. The installer's own start check (`install_block01.sh:638`) imports `PySide6.QtWidgets`, and `qt/__init__.py` imports `PySide6`/`shiboken6`. | **Yes, P1**: a fresh `bash scripts/install_block01.sh` builds an env without PySide6 and stops at "the app does not start". RT2 was accepted on `fusion_test2`, into which PySide6 was pip-installed by hand (b22cdcd), so this path was never exercised. |
| B | `scripts/benchmark_step1_gpu_demo.py:920-921` reads `QtCore.PYQT_VERSION_STR` and `QtCore.QT_VERSION_STR`. | Probed on PySide6 6.11.3: neither attribute exists (`hasattr` → False). `_environment()` runs at the end of a benchmark (`:953`), so a run does its work and then dies with AttributeError before writing the result. | Kept script, run by hand: P3. |
| C | `NOTICE:32-41` still describes PyQt5's GPL and "a planned move to PySide6". | text | Licence statement, user's wording (§4 Q1). |
| D | Test-session exit crash in two modules (`docs/v17_pyside6_plan.md` §8 item 8, §9): recorded on the Linux A9 server after all tests passed, attributed then to `viewer/scheduler.py` workers alive at interpreter shutdown (root cause not re-confirmed). cd91c5a moved the fixtures to the product close paths; the crash stayed. | On this Mac both modules (`tests/test_step1_shared_camera.py`: 15 passed; `tests/test_step1_gpu_takeover.py`: 2 passed, 45 skipped) **exit 0** — the GPU/viewer-core part does not run here (no `fusion_viewer_core`, no GL). Not reproducible without the Linux server. | No (tests only; product open/close was clean in M3). |
| E | PyQt5 still installed in the user's daily env `fusion_test2` (b22cdcd: "PyQt5 left in place"). | commit message | Not a repo change. |
| G | **The installer accepts glibc 2.28, PySide6 6.11.3 needs 2.34.** | `install_block01.sh:145-150` refuses only glibc < 2.28. PyPI has PySide6-Essentials/shiboken6 6.11.3 for Linux only as `cp310-abi3-manylinux_2_34_x86_64` (probed with `pip download --platform …`: no match for `manylinux_2_28`; the last 2_28 wheels are 6.9.3). | **Yes, P2**: on glibc 2.28–2.33 (e.g. Ubuntu 20.04 = 2.31, RHEL/Rocky 8 = 2.28) the pre-check passes and the pip step fails late with "No matching distribution found". Ubuntu 22.04 (2.35) and 24.04 are fine. |
| F | Windows not run (plan §5 M3, user runs it). docs/v17_windows_acceptance.md: native Windows lacks an installer, a launcher and an importable viewer core. | doc | Out of this block (packaging = desktop UI plan phase 5). |

Already done (checked): CI (`.github/workflows/block01_cpu_tests.yml` imports
PySide6), `envs/ci/requirements-cpu.txt`, `environment.yml`, README (131
mentions PyQt5 only as what was replaced), installer system libs
(`libxcb-cursor0`, cd91c5a), engine identity (A0.5: `engine_identity` no
longer hashes the env files; `env_lock_hash` is provenance only,
`seg_runner/runner.py:32-38, 98`). No tracked product/test/script file
imports PyQt5 (`tests/test_qt_import_point.py` enforces it).

Seen while probing, advisory only (not proposed): PySide6 prints
`RuntimeWarning: libpyside: Failed to disconnect ...` where PyQt5 raised the
`TypeError` the code already catches (e.g. `ui/main_window.py:7248`,
`_stop_montage_gpu` when the montage never connected; 15 warnings in
test_step1_shared_camera). Behaviour is unchanged; only console noise.

## 2. Proposed changes (closed whitelist)

**A1 — `envs/fusion_mesmer/requirements-pip.txt`**: delete the three
`pyqt5*` lines; add `pyside6-essentials==6.11.3` and `shiboken6==6.11.3`
in the exporter's format (lower-case, `_`→`-`, sorted position). The usual
way is to re-export from the real env (`scripts/export_fusion_mesmer_env.sh`),
but the server is down and `fusion_test2` still has PyQt5, so the edit is
made by hand to exactly what a re-export of an env with PySide6 and without
PyQt5 would write. Nothing else in the list changes. Nothing in the list
depends on PyQt5 (qtpy 2.4.3 picks any installed binding; matplotlib does
not require one).
* Effect: the installer's `lock_id` (`install_block01.sh:246`) changes, so
  the next install builds a **new** env prefix (`fusion_mesmer-<new id>`,
  full download, ~160 conda + ~220 pip packages). Existing prefixes are not
  touched. Existing installs that pulled RT2 code without PySide6 are broken
  today; re-running the installer fixes them.
* Effect: `env_lock_hash` in new runs' provenance changes. It is not part of
  the engine identity, so existing pre-segmentation results stay valid.
* `conda-linux-64.lock` has no Qt package (grep) and is not changed.

**A2 — `tests/test_qt_import_point.py`** (one new test): the installer's pip
list pins the binding `qt/__init__.py` names — no `pyqt5*`/`pyqt6*`/`pyside2`
line, and `pyside6-essentials`/`shiboken6` pinned to the same version as in
`envs/ci/requirements-cpu.txt` and `envs/fusion_mesmer/environment.yml`.
Text-only, runs anywhere.

**A3 — `scripts/install_block01.sh:145-150`** (and its two messages): the
glibc floor becomes 2.34, with the reason in the message ("2.34 or newer
needed: the interface's Qt for Python packages are built for it"). Nothing
else in the installer changes. The stated support range follows:
`docs/v17_install_plan.md:122` ("glibc ≥ 2.28" → 2.34, with a dated note;
`:90` is about the viewer-core wheel's own tag and stays). Test: one new
case in `tests/test_install_scripts.py` (already in CI), through its
`_preflight` harness with `uname`/`getconf` stubbed as shell functions:
glibc 2.31 → refused with the 2.34 message, 2.34 and 2.35 → accepted.
On the Mac the harness needs a bash ≥ 4 (the installer uses associative
arrays; `/bin/bash` is 3.2) and `flock` stubbed too; codex round 2 ran it
that way with A3 applied in memory: 2.31 → 1, 2.34 → 0, 2.35 → 0. On Linux
CI nothing extra is needed. Alternative for the user (§4 Q3): keep
2.28 and pin an older PySide6 (6.9.3) — not recommended: it would make the
product env differ from the accepted RT2 build.

**B — `scripts/benchmark_step1_gpu_demo.py:920-921` + `qt/__init__.py`**:
`qt/__init__.py` (the one file allowed to name the binding) gains
`BINDING_VERSION = PySide6.__version__`; the script records
`"qt_binding": f"{qt.BINDING} {qt.BINDING_VERSION}"` and
`"qt": QtCore.qVersion()`. The key `pyqt` is renamed to `qt_binding`
(old reports under docs/benchmarks keep theirs; nothing reads the key).
Test: a new case in `tests/test_qt_import_point.py` loads the script and
calls the real `_environment(SimpleNamespace(platform="offscreen"))` (it
reads only `args.platform`; no GL — codex reproduced the AttributeError this
way) and checks the returned `qt_binding` / `qt` fields.

**A4 — `tests/ci_cpu_modules.txt`**: one line, `test_qt_import_point.py`,
so the new guards run in CI (today CI runs only the listed modules,
`scripts/ci_run_modules.sh:15`). It runs offscreen without GL; run on the
Mac first to confirm it passes in one process.

**C — `NOTICE:32-41`**: replace the PyQt5 paragraph with the PySide6 one,
wording per the user's ruling (§4 Q1). Draft: "The interface uses PySide6
and shiboken6 (Qt for Python), licensed under the LGPL v3 (and other
licences offered by The Qt Company). The installer fetches them from PyPI
onto the user's machine as separate, replaceable packages; this repository
does not include them." No claim beyond that.

**D — `KNOWN_ISSUES.md`** (Tests section), one line each, facts only:
* the interpreter-exit crash of `test_step1_shared_camera` and
  `test_step1_gpu_takeover` recorded on the Linux A9 server with the viewer
  core and GL (docs/v17_pyside6_plan.md §8, cd91c5a), root cause not
  re-confirmed; on the Mac (no viewer core, offscreen) both exit 0:
  15 passed, and 2 passed / 45 skipped;
* on macOS with PySide6 6.11.3 the repo conftest's between-test `gc`
  sometimes segfaults inside shiboken6 (seen on unchanged HEAD; run one test
  per process there).

**E — `docs/v17_pyside6_plan.md`**: status v0.5 (M4 done except D/E/F),
§9 updated. This plan gets its execution record.

Product code: only the Qt entry point `qt/__init__.py` gains the version
constant (B). Not changed: `ui/`, `viewer/`, `workers/`, `core/`, the
conftest, the installer script beyond A3, the CI workflow (A4 changes only the module list), `environment.yml`, the conda lock, the
Step2 block's files.

## 3. Deferred, each needs the server or its own ruling

* **D (exit crash)**: investigate on the Linux server when it is back
  (which thread is alive, whether `TileScheduler.shutdown()` is reached from
  `Step1WholeSlideMount.close`). Only test fixtures may change; a product
  change would be its own block.
* **E (uninstall PyQt5 from fusion_test2)**: one command on the server
  (`pip uninstall -y pyqt5 pyqt5-qt5 pyqt5-sip`), after A is verified there.
  Your machine, your call.
* **F (Windows)**: unchanged — the user's run via WSL2, or a native-Windows
  installer/launcher as part of the packaging phase (separate plan).
* The disconnect warnings (advisory above): only if the user wants them
  silenced.

## 4. Questions for the user

1. NOTICE wording: the draft in §2 C, or your own text? (Licence text is
   yours to decide; I do not give legal advice.)
2. Is A's hand edit acceptable now, or should it wait for a real re-export
   on the server? (Recommended: edit now — the installer is broken today —
   and verify on the server per §5.)
3. glibc floor (G): raise the installer's minimum to 2.34 (recommended), or
   stay at 2.28 with an older PySide6?

## 5. Tests and verification

* On the Mac (block01_mac env, one process per test as usual):
  `tests/test_qt_import_point.py` (old + new cases), `tests/test_step1_gpu_probe.py`
  (whatever runs without GL). Mutation: restore a `pyqt5==` line / drop
  `shiboken6` / restore `PYQT_VERSION_STR` → the new tests fail.
* Wheels exist for the server: done during the investigation —
  `pip download --no-deps --only-binary=:all: --platform
  manylinux_2_34_x86_64 --python-version 3.10 pyside6-essentials==6.11.3
  shiboken6==6.11.3` fetched both `cp310-abi3-manylinux_2_34_x86_64` wheels
  into the scratchpad; with `manylinux_2_28` there is no match.
* **Real verification needs the Linux server (user acceptance):**
  `bash scripts/rebuild_fusion_mesmer_env_check.sh <new absolute temp path>` (fresh env from the lock
  + pip list, `pip check`, set comparison), then a fresh
  `bash scripts/install_block01.sh --root <new dir>` from a **separate
  checkout** (the installer rewrites that checkout's install record, which
  `scripts/block01` uses — keep the daily checkout pointing at its env) reaching
  "app start check: … accepted", and `scripts/block01` starting and closing.
  Automated tests do not replace this.

## 6. Risks and rollback

* A wrong pin in A breaks fresh installs again, the same as today; caught by
  the A2 test (consistency) and the server rebuild (real).
* New env prefix = a full download for anyone who re-runs the installer
  (expected; the old prefix stays and can be removed by hand).
* Rollback: revert the commit; no data or project files are touched. A
  checkout that already ran the new installer keeps pointing at the new env
  through its install record (`install_block01.sh:655`, read by
  `scripts/block01:31`): re-run the installer after the revert (it finds the
  old, complete prefix and reuses it) to point it back.

## 7. Size

A + G + B + C + D + E: about half a day here, plus the user's server check.

## 8. Codex plan review round 1 (astra high) — FAIL, taken (v1.2)

* P1 (A) confirmed: the installer builds from the lock + `requirements-pip.txt`,
  not `environment.yml`. Kept.
* P2 glibc 2.34 — found independently; already item G in v1.1. Added the
  support-range doc and the preflight test (A3), corrected the download
  check to `manylinux_2_34`.
* P3 benchmark test: the weak fallback removed; the real `_environment()`
  is called (B).
* P3 rebuild command lacked its required path argument: fixed (§5).
* P3 new tests not in CI: `tests/ci_cpu_modules.txt` added (A4).
* Advisories taken: D worded with counts and environment, root cause not
  claimed; rollback explains the install record; acceptance in a separate
  checkout; "no product code" corrected to the one Qt-entry constant.
  Historical evidence under `docs/midpan_real_x_evidence/` keeps its PyQt5
  imports (a past experiment, not a product path) — not changed.

## 9. Codex plan review round 2 (astra high) — PASS

No new public-path defect; every round-1 point taken correctly; whitelist
closed. Advisories applied in v1.3: the macOS run conditions of the new
preflight test, §1 D worded as not re-confirmed, "CI workflow" vs the module
list. Per AGENTS.md rule 7, hardening of the plan stops here. Awaiting the
user's approval (and answers to §4).

## 10. User ruling and execution record (2026-11-08)

User (TG): "可以，我们后续再开发兼容旧系统的版本" — fix the installer now
with the 2.34 floor (§4 Q2 = edit now, Q3 = 2.34); older-Linux support comes
later as its own block. Investigated for that block: PyPI PySide6 wheels are
manylinux_2_28 up to 6.9.3 and manylinux_2_34 from 6.10.0; conda-forge
`pyside6` is built for `__glibc >=2.17`, newest with a Python 3.10 build
6.11.2. Q1 (NOTICE): the §2 C draft was used; the user can change the
wording before the commit.

Done (whitelist as §2, nothing else):
* A1 `envs/fusion_mesmer/requirements-pip.txt`: 3 `pyqt5*` lines out,
  `pyside6-essentials==6.11.3`, `shiboken6==6.11.3` in (sorted).
* A2 `tests/test_qt_import_point.py::test_the_installers_pip_list_installs_the_binding_the_import_point_names`.
* A3 `scripts/install_block01.sh` floor 2.34 + message (`bash -n` ok);
  `docs/v17_install_plan.md:122`; test
  `tests/test_install_scripts.py::test_the_glibc_floor_is_what_the_qt_wheels_need`.
* A4 `tests/ci_cpu_modules.txt`: `test_qt_import_point.py`.
* B `qt/__init__.py` `BINDING_VERSION`; `scripts/benchmark_step1_gpu_demo.py`
  records `qt_binding` / `qVersion()`; test
  `tests/test_qt_import_point.py::test_the_gpu_benchmark_records_the_binding_it_runs_on`.
* C NOTICE (draft text). D KNOWN_ISSUES (exit crash, macOS notes, the 2.34
  floor and the conda-forge route). E docs/v17_pyside6_plan.md v0.5.

Tests on the Mac (block01_mac env, CI-style `block01` link in the scratchpad):
* `test_qt_import_point.py`: 8 passed (without the link, the existing
  subprocess test fails here, same on HEAD; CI makes the link).
* `test_install_scripts.py` with Homebrew bash 5.3: 21 passed, 10 failed —
  the same 10 fail on HEAD (Linux tools); the new case passes.
* `import block01.main` offscreen: ok, `PySide6 6.11.3`.
* Mutations (scratch copy), each turns its test red: `pyqt5==` back in the
  pip list; `shiboken6` dropped; floor back to 2.28; `QT_VERSION_STR` back.

Not done here (needs the Linux server — user acceptance): the rebuild check,
a fresh install from a separate checkout, `scripts/block01` start/close;
PyQt5 out of fusion_test2; the exit-crash investigation. Not committed.

Code review round 1 (astra low, 2026-11-09): PASS, no P1/P2. P3 taken: the
glibc test also asserts exit 0 for 2.34/2.35. (A first run was cut off by a
session restart and left no output; rerun.)

## 11. Linux wrap-up on Ubuntu 22.04 (backlog 7, 2026-11-12)

Run by the user's Ubuntu agent on a fresh clone of v17 at 43e18b7
(glibc 2.35, RTX 2080 Ti, X11), checks only:
* rebuild check (`scripts/rebuild_fusion_mesmer_env_check.sh`): conda
  identical (161), all 220 pip pins, `pip check` clean;
* fresh install into a new root: the 6.11.3 route (no `qt69` prefix),
  self-test ok for Cellpose / StarDist / Mesmer on the GPU, app start check
  accepted, `BLOCK01_QT_VERSION=6.11.3` in the record; the same command again:
  "already installed", no rebuild;
* `scripts/block01` start and close, Step0-Step4 opened: 3 of 3 exit 0;
* `test_install_scripts.py` + `test_qt_import_point.py`: 65 passed;
* `test_step1_shared_camera.py` and `test_step1_gpu_takeover.py` on the
  desktop, 3 runs each: all exit 0 -- the interpreter-exit crash of §3 D was
  not reproduced (34 GPU-gated cases were not run).
Left, by the user's ruling: PyQt5 out of `fusion_test2` waits for the old
server; nothing else.

