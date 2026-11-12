# Older Linux (glibc 2.28–2.33): PySide6 6.9.3 from PyPI (backlog 6) — plan v2.1

Started by the user (TG) 2026-11-11: "做第6项吧，请生成计划给codex审核".
Backlog 6: older Linux (glibc < 2.34). Follows the PySide6 wrap-up
(`docs/v17_pyside6_wrapup_plan.md` §10): the user raised the installer's
floor to 2.34 then, and put older-Linux support into a block of its own.

History: v1.x (conda-forge's PySide6 6.11.2 in a second conda lock, codex
round 1 PASS) is replaced. The user asked whether older machines could fall
back to PyQt5; the answer was no (GPL licence, two bindings, Qt 5 end of
life, and the floor would still be 2.28), and instead PyPI's PySide6 6.9.3,
the last release with `manylinux_2_28` wheels, was checked on the Mac
(§1.3). The user (TG) agreed to this route: "同意，请执行".

## 1. Today (measured 2026-11-11, read-only)

### 1.1 The installer (`scripts/install_block01.sh`)

* `preflight` (:148-155) refuses glibc < 2.34. The reason is PySide6:
  PyPI's 6.10+ Linux wheels are `manylinux_2_34` only.
* `ensure_environment` (:279-317) builds one environment from
  `envs/fusion_mesmer/conda-linux-64.lock` plus `requirements-pip.txt`
  (`pip --no-deps`, then `pip check`). That list pins
  `pyside6-essentials==6.11.3` (:160) and `shiboken6==6.11.3` (:181).
* `lock_id` hashes the lock and the pip list; `env_prefix` is
  `$ROOT/envs/fusion_mesmer-<id>`. Before removing an interrupted prefix the
  guard accepts only `"$ROOT"/envs/fusion_mesmer-????????`.
* `check_system_libs` (:110-136) needs 23 system X / GL libraries, because
  the PyPI wheels bring Qt but not the X libraries. The hint names
  Ubuntu/Debian packages only.
* The self-test's start check imports `PySide6.QtWidgets` and
  `block01.main` offscreen.
* `tests/test_qt_import_point.py::test_the_installers_pip_list_installs_the_binding_the_import_point_names`
  requires the running binding's version to equal the pip list's, CI's
  and `environment.yml`'s pin (6.11.3).

### 1.2 What else sets the glibc floor

Lowest glibc each pinned binary needs (PyPI / pytorch.org file tags,
conda-forge `__glibc` dependencies; scan scripts in the scratchpad `g6/`):

| glibc | what |
|---|---|
| 2.34 | `pyside6-essentials`, `shiboken6` 6.11.3 |
| **2.28** | `pyside6-essentials`, `shiboken6` **6.9.3**; viewer core `fusion_viewer_core-0.2.1-…-manylinux_2_28` (`wheels.lock`); `h5py` 3.16.0; `pyarrow` 25.0.1; `nvidia-nvimgcodec-cu12` 0.7.0.11; the SpatialData env's `h5py` 3.16.0 |
| 2.27 and lower | everything else in both pip lists with a Python 3.10 Linux wheel |
| unknown | torch 2.5.1, torchvision 0.20.1, torchaudio 2.5.1, all +cu121 (tagged only `linux_x86_64`); `asciitree`, `deepcell`, `deepcell-toolbox`, `deepcell-tracking` (source only: built on the machine) |
| 2.17 | the conda-forge packages of both locks that declare `__glibc`; the others declare none |

So the floor is **2.28 by the packages' own tags**: a candidate until a
fresh install on glibc 2.28 passes (§5). Rocky / Alma / RHEL 8 (2.28),
Debian 10 (2.28), Ubuntu 20.04 (2.31). CentOS 7 (2.17) would need older
h5py, pyarrow and viewer-core builds: out of scope.

### 1.3 PySide6 6.9.3 (checked on the Mac and in its Linux wheels)

* **The code runs on it.** `pyside6-essentials` and `shiboken6` 6.9.3 were
  installed into a scratch folder put first on `PYTHONPATH` (the Mac
  environment untouched). `import block01.main` offscreen reports
  "binding PySide6 6.9.3".
* **All 370 test files** ran once on 6.9.3 and once on 6.11.3 (one process
  per file), and the files whose results differed were run again one
  process per test:
  * Every test that fails on 6.9.3 also fails on 6.11.3 (failures this Mac
    already has).
  * The one 6.9.3-only failure is the version-pin test of §1.1, which by
    design fails on any other version.
  * `test_step0_tissue_preview` fails one run in three on both versions
    (unrelated to the version).
  * On 6.11.3 four whole-file runs crashed (exit 139); split per test,
    their results match 6.9.3's.
* **Its Linux wheels** (`…-cp39-abi3-manylinux_2_28_x86_64.whl`), unpacked:
  the newest symbol versions they ask for are `GLIBC_2.28` and
  `GLIBCXX_3.4.21`.
* **System libraries.** For the modules the app loads (QtCore, QtGui,
  QtWidgets, QtOpenGL, QtOpenGLWidgets, QtSvg, QtTest) and the `xcb` and
  `offscreen` platform plugins, following each library's `NEEDED` entries
  through the wheel: the libraries from outside the wheel are the same set
  for 6.9.3 and 6.11.3 (6.9.3 also names `librt.so.1`, part of glibc). So
  today's `QT_SYSTEM_LIBS` list fits both versions.
* **`libxcb-cursor.so.0`** (needed by Qt ≥ 6.5's xcb plugin): Ubuntu 20.04
  has `libxcb-cursor0`; on RHEL / Rocky 8 it is `xcb-util-cursor` from EPEL
  8, not the base system (public package listings and forum reports, not
  yet tried here: §5).

Not checked on the Mac: Linux-only behaviour (the xcb window, the
system's fonts and GL), which acceptance covers (§5).

## 2. Decisions for the user

* **Q1 — the route: done.** Older Linux gets PyPI's PySide6 6.9.3 in a
  second environment; glibc ≥ 2.34 keeps 6.11.3 exactly as accepted.
* **Q2 — the new floor: 2.28** (§1.2). Lower needs other package versions:
  a block of its own if ever wanted.
* **Q3 — where to accept.**
  * The install needs a fresh glibc 2.28 system: a Rocky 8 container on the
    server is enough. Ubuntu 20.04 (2.31) alone would not prove 2.28.
  * The look needs a desktop. `--older-qt` (§3.2) lets the Ubuntu server
    run the 6.9.3 environment too.

## 3. Design

### 3.1 The second pin list

* New `envs/fusion_mesmer/requirements-pip-older-linux.txt`: a comment
  saying why, and exactly two lines, `pyside6-essentials==6.9.3` and
  `shiboken6==6.9.3`.
* On the older-Linux route the installer gives pip
  `requirements-pip.txt` with its two Qt lines replaced by these two. It
  writes that list beside the prefix (`<prefix>.requirements-pip.txt`, next
  to its `.owner` file), before the conda part, and keeps it there for the
  record. If the main list does not have
  exactly one line for each of the two names, it stops ("the pip list
  changed: update the older-Linux pins").
* The conda lock is the same for both routes. Nothing else in the pip list
  changes. `pip check` still runs.

### 3.2 The installer

* `preflight`, glibc:
  * ≥ 2.34 → Qt 6.11.3, as today.
  * 2.28–2.33 → Qt 6.9.3. It says: "glibc X: the interface uses PySide6
    6.9.3 (the last release built for this glibc)".
  * < 2.28 → refused: "2.28 or newer needed (PySide6, the viewer core,
    h5py and pyarrow packages are built for it)".
* New option `--older-qt`: the 6.9.3 route on a newer glibc too (for the
  look check on the Ubuntu server, §5). Usage text updated.
* `lock_id` / `env_prefix`:
  * The 6.11.3 route: unchanged, byte for byte, so existing installs stay
    "complete".
  * The 6.9.3 route: `$ROOT/envs/fusion_mesmer-qt69-<id>`, where `<id>`
    hashes the lock, the main pip list and the older-Linux list.
  * The space check and the prefix states use the route's prefix.
  * The removal guard accepts both name patterns, and nothing else.
* `check_system_libs`: the same list on both routes (§1.3). The hint gains
  a line for RHEL / Rocky: install the packages that provide these files
  with `dnf`; `libxcb-cursor.so.0` is `xcb-util-cursor` from EPEL.
* `ensure_environment`: on the 6.9.3 route, pip takes the list of §3.1.
* `write_record`: one more line, `BLOCK01_QT_VERSION=6.11.3|6.9.3`. The
  launcher does not read it; it is for support.
* `scripts/block01` (launcher): no change. It runs the recorded prefix.
* The self-test: no change. Its start check stays the final word on
  whether the interface loads.

### 3.3 The version-pin test

`test_the_installers_pip_list_installs_the_binding_the_import_point_names`
keeps its checks of the main list, CI and `environment.yml` (all 6.11.3),
and adds: the older-Linux list has exactly the two names, both at one
version; the running binding's version is one of the two lists' versions.
So it passes in either environment and still catches a list that drifts.

### 3.4 Keeping 6.9.3 working

From now on the code must keep running on 6.9.3. The README of
`envs/fusion_mesmer/` gets the command that repeats §1.3's check on the Mac
(the two wheels into a scratch folder, put first on `PYTHONPATH`, the
tests run). It is a manual check before a release, not a new CI job.

### 3.5 Not changed

The app code, `qt/__init__.py`, the conda lock, the 6.11.3 environment,
the SpatialData environment, CI (`envs/ci/requirements-cpu.txt`),
`environment.yml`, the Mac environment.

## 4. Files (closed list)

* New: `envs/fusion_mesmer/requirements-pip-older-linux.txt`.
* `scripts/install_block01.sh`: `preflight` glibc branch, `--older-qt` and
  the usage text, `lock_id` / `env_prefix` / the removal guard, the
  `check_system_libs` hint, `ensure_environment` (the pip list),
  `write_record` (one line).
* `tests/test_install_scripts.py`: the glibc test extended; new cases (§5).
* `tests/test_qt_import_point.py`: the version-pin test (§3.3).
* Docs:
  * `envs/fusion_mesmer/README.md`: the second list, §3.4's check.
  * `docs/v17_install_plan.md`: the support range.
  * `KNOWN_ISSUES.md`: the 2.34 note becomes the two routes.
  * `README.md`: the install requirements line.
  * This plan. (`docs/v17_older_linux_conda_qt_plan.md`, v1.x, untracked,
    is removed: its findings are in §1.)

## 5. Tests and acceptance

Automated (offline, `tests/test_install_scripts.py`). Each case uses the
harness that can see it: glibc and route, `_preflight` (`uname` /
`getconf` stubbed); options, `main --check-only` with the same stubs;
prefix names, the real `lock_id` / `env_prefix`; libraries, the existing
`ldconfig` test; the pip list, `_ensure_env` (command stubs record the
calls). On the Mac, Homebrew bash 5 and stubs, as in the wrap-up.

1. glibc 2.27 → refused with the 2.28 message.
2. glibc 2.28, 2.31, 2.33 → accepted, Qt 6.9.3.
3. glibc 2.34, 2.35 → accepted, Qt 6.11.3.
4. `--older-qt` on 2.35 → Qt 6.9.3.
5. `env_prefix` per route; the 6.11.3 name is byte-identical to today's.
6. The removal guard accepts both patterns and refuses others.
7. The 6.9.3 route's pip list is the main one with exactly the two Qt lines
   replaced; a main list missing one of them stops the install.
8. `ensure_environment` on the 6.9.3 route gives pip that list; on the
   6.11.3 route, `requirements-pip.txt` as today.
9. The record has the `BLOCK01_QT_VERSION` line per route.
10. The missing-library hint has the RHEL / EPEL line.
11. The existing prefix-state and space tests (absent, interrupted,
    foreign, built, complete; the space left to do) run on both routes.
    With a complete 6.11.3 prefix in the same root, a 6.9.3-route run
    reuses or removes only its own `qt69` prefix and leaves the other as
    it was.

Plus `tests/test_qt_import_point.py` on 6.11.3 (the Mac env) and on 6.9.3
(the scratch folder first on `PYTHONPATH`). The `test_install_scripts.py`
cases that fail on HEAD on the Mac (Linux tools) are listed by name
before the change (the baseline); after it, the same ones fail and no
other, and the new ones pass. On Linux, acceptance step 1 also runs the
whole `tests/test_install_scripts.py`.

Needs Linux — the user's acceptance:

1. A fresh Rocky 8 container (glibc 2.28) on the server, new install root,
   empty package cache, the Qt system libraries installed per the hint
   (EPEL for `xcb-util-cursor`):
   * `bash scripts/install_block01.sh` completes, including the compiled
     package (`deepcell-toolbox`);
   * the self-test passes, and the app start check accepts;
   * `tests/test_install_scripts.py` in full passes there;
   * torch, torchvision and torchaudio each load their native parts (this
     settles their glibc floor, §1.2). GPU is optional.
2. The Ubuntu server with `--older-qt`: install, start the app on its
   desktop, open Step0, Step1, Step4; compare with the 6.11.3 install.
3. An existing 6.11.3 install re-run: "environment already installed", no
   rebuild.

## 6. Risks and rollback

* **Two Qt versions.** A later change could use something new in 6.10+;
  §3.4's check catches it on the Mac. Behaviour that differs only on Linux
  is covered by acceptance step 2 now, not later.
* **6.9.3 gets no more fixes** from Qt. Only older machines use it.
* **EPEL on RHEL / Rocky 8**: one more repository to enable for one
  library. The installer only names it; it installs nothing system-wide.
* **Rollback:** revert the commit. Existing 6.11.3 installs keep their
  prefix and are not rebuilt. On an older machine, the reverted installer
  refuses it again (2.34). Its `fusion_mesmer-qt69-…` prefix is still
  recorded in the checkout's `.block01/install.env`: delete that prefix
  only together with the record, otherwise the launcher points at a
  missing environment.

## 7. Plan review (codex astra low)

Round 1 PASS, no P1/P2 (raw output: scratchpad
`codex_g6_plan_v2_r1.txt`). Three P3s, all taken in v2.1:
1. The prefix-state and space tests run on both routes, with a complete
   6.11.3 prefix beside the 6.9.3 one (§5 case 11).
2. The Mac's failing installer tests are named before the change; no new
   failure is allowed; the full file runs on Linux at acceptance.
3. §1.2 gives torchvision its own version (0.20.1).

## 8. Implementation (uncommitted, 2026-11-11)

Files as §4. In `scripts/install_block01.sh`: `OLDER_QT`, `OLDER_PINS`,
`glibc_at_least`, `lock_id` / `env_prefix` per route, `pip_list`,
`qt_version`, `write_older_pip_list`, the removal guard's second pattern,
the RHEL / EPEL hint lines, `--older-qt` and the header / usage text,
`BLOCK01_QT_VERSION` in the record.

Tests on the Mac (Homebrew bash 5; the Mac's `date`, `realpath` and
`sha256sum` are not GNU's, so a scratch folder of stand-ins goes first on
`PATH` for the test run only: `date -Is`, `realpath -m`, `shasum -a 256`):
* Baseline before the change, `test_install_scripts.py`: 4 fail, all for
  Mac reasons (no `flock` / `setsid`, `uname` is Darwin):
  `test_a_licence_needs_an_answer`,
  `test_the_space_check_counts_only_the_work_left`,
  `test_install_caches_stay_under_the_root`,
  `test_the_space_check_counts_the_spatialdata_environment_left`.
* After: the same 4 fail, no other; 26 new cases pass, replacing the old glibc test (57 in the file; 4 of them added after the code review).
  §5 cases 1–11 map to `test_the_glibc_decides_the_qt_route` (6),
  `test_older_qt_picks_the_6_9_route_on_a_newer_glibc`,
  `test_each_route_has_its_own_prefix_and_the_6_11_one_is_unchanged`,
  `test_the_removal_guard_takes_only_the_two_names` (with the interrupted
  `qt69` prefix of case 11), `test_the_6_9_pip_list_replaces_exactly_the_two_qt_lines`,
  `test_the_older_pin_list_is_exactly_the_two_qt_pins`,
  `test_ensure_environment_gives_pip_the_routes_list` (2),
  `test_the_record_names_the_qt_version` (2),
  `test_the_missing_library_hint_names_epel_for_rhel`,
  `test_the_6_9_route_keeps_to_its_own_prefix` (5),
  `test_the_space_check_counts_the_6_9_environment_left`.
* `test_qt_import_point.py`: 8 pass on 6.11.3 and 8 on 6.9.3; with a
  drifted 6.9.x pin the version-pin test fails, as it should.

Code review (codex astra low, raw output scratchpad `codex_g6_code_r1.txt`):
round 1 PASS, no P1/P2. Three P3 test advisories:
1. Taken: `test_a_changed_main_list_stops_the_6_9_route_before_any_environment`
   (each Qt name missing or doubled: the install stops, no micromamba or
   pip call, no environment directory).
2. Taken in part: the 6.9.3 space test also covers a complete prefix and a
   refusal for lack of space.
3. Not taken: a byte comparison of the whole record with the old script's.
   The diff adds one `printf` line to `write_record` and changes nothing
   else in it.

Acceptance: the user (TG, 2026-11-11) reported the Linux acceptance of §5
passed: "验收通过，可以提交推送了".
