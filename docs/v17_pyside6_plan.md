# v17 — PyQt5 → PySide6 move (plan v0.4, 2026-11-07)

v0.4: **M0–M3 done** (PySide6 6.11.3-Essentials). What the move cost, per
area, is in §8 — the two findings that changed code shape are that a Qt 6
binding CANNOT reach past a subclass signal that shadows `QThread.finished`
(the business payload moved to `job_finished`) and that `libxcb-cursor0` is a
runtime dependency of Qt ≥ 6.5 on X11.

v0.3: user rulings (TG 2026-11-04) — **no switch back to PyQt5** (the old
version is on GitHub: 5501cf9 is the last PyQt5 commit); **the user runs the
Windows tests**; **unused old scripts deleted** (30 files, §3).

v0.2: codex review (low) taken — old GPU layer is still in use (decision 1
reworded), two removed APIs and the QThread.finished bypass added, sip count
corrected.

Part of the cross-platform desktop UI (docs/v17_desktop_ui_plan.md §6/§8).
User ruling 2026-11-03: theme on PyQt5 (done, 5501cf9) → **PySide6** → page by
page. This document is the plan only; no code moves before the user says so.

## 1. Why

* **Licence.** PyQt5 is GPL or a paid commercial licence; PySide6 is LGPL,
  which lets the app be handed to companies without a per-seat Qt-binding
  licence.
* **Qt 6.** Maintained (Qt 5.15 open-source support ended), better Windows
  high-DPI handling and the same code on Windows and Ubuntu.

## 2. Rules for the move

1. **Nothing the user sees changes.** Same layout, sizes, colours, behaviour,
   shortcuts; the theme from phase 1 stays as it is.
2. **No speed loss.** Step0 load/first frame, pan/zoom, Step1 entry,
   Step2/3/4 runs are measured before and after on the A9 server (SSD and HDD,
   per deployment-targets) and must stay within noise.
3. **The Rust core is not touched.** It takes a GL function loader
   (`core_gl_session.attach_gl`); only the Qt side of that call moves.
4. Every stage ends with the full test suite equal to the baseline (same
   pre-existing failures, nothing new), a codex review, and the user's
   real-machine check where the stage touches what is on screen.

## 3. What there is to move (measured 2026-11-04 at 5501cf9)

| Item | Count | Treatment |
|---|---|---|
| product files importing PyQt5 (ui 74, workers 7, utils 5, viewer 3, core 1, main) | 91 | through one import point (§4) |
| test files importing PyQt5 | 230 | same import point |
| scripts importing PyQt5 | 48 → 18 | 30 unused ones deleted (none referenced by product, tests or kept scripts); the 18 kept move with the rest |
| `pyqtSignal` / `pyqtSlot` | 312 / 2 | `Signal` / `Slot` |
| `sip.isdeleted` (main_window, step0_page ×4, compare_strip, step4_page ×2) | 8 calls | `is_deleted()` helper (shiboken6.isValid inverted) |
| `sip.setdestroyonexit(False)` (utils/perf_dispatch.py:79) | 1 | assess: PySide6 has no equivalent; check shutdown order |
| `exec_()` | 41 | `exec()` |
| `int(<Qt flag>)` (regions/tools.py:282, regions/overlay.py:197/211, step0/overview_panel.py:1151-1159) | 7 | **fails under PySide6** (probe below); use `.value` via a helper |
| `QOpenGLWidget` (step1_core_layer, step1_core_labels, step3_core_labels, step0/gl_host, montage_gpu, regions overlay, old Python GPU layer) | 21 refs | `QtOpenGLWidgets.QOpenGLWidget` |
| `QOpenGLFramebufferObject(+Format)` (step0/gl_host.py) | 5 | moves to `QtOpenGL` |
| `QShortcut`, `QActionGroup`, `QAction` | 6 / 1 / 1 | move to `QtGui` |
| mouse/wheel event positions (`.pos()`, `angleDelta`) | ~35 | `.pos()` still exists in Qt 6 (deprecated); keep, review the wheel ones |
| `QFontMetricsF.width()` (regions/overlay.py:299, label painting) | 1 | **removed in Qt 6** → `horizontalAdvance()` |
| `setsize()` / `byteCount()` in Step0's pixel-test helper (step0/gl_host.py:1270) | 1 | PySide6 buffer API, `sizeInBytes()` |
| `QThread.finished.__get__(…)` bypass of shadowed signals (main_window.py:12098, step0/step0_page.py:3268) | 2 | binding-sensitive: targeted finish/cancel/close tests in M2 |
| PyQt version metadata (scripts/benchmark_step1_gpu_demo.py:904) | 1 | binding-neutral |
| dependency files: envs/fusion_mesmer/{environment.yml,requirements-pip.txt}, envs/ci/requirements-cpu.txt, scripts/install_block01.sh:637, .github/workflows/block01_cpu_tests.yml:61-63, README | 6 | PySide6-Essentials pinned |

**Probe (scratch install of PySide6-Essentials 6.8.1, nothing in the product
env):** pyqtgraph 0.14.0 runs on it (`PYQTGRAPH_QT_LIB=PySide6`, PlotWidget
OK); `QOpenGLWidget` + `getProcAddress` returns a usable int; an off-screen
`QOpenGLFramebufferObject` is valid; `shiboken6.isValid` tracks deletion;
`int(Qt.ShiftModifier)` raises — confirming the flag sites above.

**Speed probe:** a signal emit costs ~3.5 µs on PySide6 vs ~1.5 µs on PyQt5;
plain widget calls ~1.9 µs vs ~0.95 µs. A frame makes a few hundred such calls
(< 1 ms), so no visible effect is expected — but rule 2 is checked on the real
paths, not assumed.

## 4. How: one import point

A small module `block01/qt.py` becomes the only place that names a Qt binding.
It exports `QtCore, QtGui, QtWidgets, Signal, Slot, QOpenGLWidget,
QOpenGLFramebufferObject(+Format), QShortcut, QActionGroup, is_deleted(),
flag_value()` and sets `PYQTGRAPH_QT_LIB` **before** pyqtgraph is imported. A
test forbids `PyQt5`/`PySide6` imports anywhere else.

**No switch** (user ruling): there is no setting to choose the binding. During
M0–M1 the module names PyQt5; M2 changes it to PySide6 once, for good. The way
back is git: 5501cf9 is the last commit on PyQt5.

Why the import point anyway: M0 and M1 are tested on the binding that works
today, so when M2 changes the binding, a failure is the binding and not the
rewrite; and the Qt 5/6 differences (flags, deleted objects, GL classes) live
in one file instead of 90.

## 5. Stages

* **M0 — import point, still PyQt5.** Add `qt.py`; rewrite imports in product,
  tests and the 18 kept scripts mechanically (script + review). Signals renamed
  through the export. Expected result: zero behaviour change; full suite equal
  to baseline.
* **M1 — the Qt 6 differences, still PyQt5.** Flag sites → `flag_value()`,
  `sip` → `is_deleted()`, `exec_` → `exec`, `QFontMetricsF.width` →
  `horizontalAdvance`, the GL/FBO/shortcut names via the exports,
  `setdestroyonexit` decision. Full suite equal to baseline.
* **M2 — PySide6.** Install PySide6-Essentials in the product env; `qt.py`
  names PySide6. Run the full suite and fix what differs (signal argument
  types, object lifetimes in lambdas, enum strictness, pyqtgraph details), with
  targeted finish/cancel/close checks for the workers that use the
  `QThread.finished` bypass. Suite equal to baseline.
* **M3 — real machine.** Ubuntu A9 server: every step, all GL viewers (Step0,
  Step1, Step3, Regions, pre-seg montage), dialogs, both themes; timings vs the
  PyQt5 build (5501cf9) on SSD and HDD (rule 2). Then **Windows, run by the
  user** (100 % and 150 % scaling), with a short checklist from me.
* **M4 — clean-up.** PyQt5 out of the env files, CI, install script, README.

Commits: M0+M1 may be pushed as one PyQt5 commit after their suite run; M2
is pushed only after the user's Ubuntu acceptance (M3).

Rough size: M0 1 day, M1 0.5–1 day, M2 1–2 days, M3 Ubuntu 0.5 day + the
user's Windows run, M4 0.5 day.

## 6. Settled

1. **Old Python GPU path** (`ui/step1_gpu_layer.py`, 2,586 lines): still in
   use — the pre-seg montage builds it directly
   (step1_presegmentation/montage_gpu.py:208) and Step1 falls back to it when
   the Rust engine cannot start (step1_viewer_mount.py:631). So it is
   **ported with the rest**. Moving the montage onto the Rust engine (and then
   removing it) stays a separate backlog item.
2. **No switch back** — git is the way back (user, 2026-11-04).
3. **Windows**: the user runs it (user, 2026-11-04).
4. **Old scripts**: 30 unused ones deleted (user, 2026-11-04).

## 7. Risks

* GL: context/function pointers, framebuffer and DPI on Windows; viewer
  teardown/recreation (step changes, dataset switch). Covered by M3 on both
  OSes and the existing GL lifecycle tests (incl. QT_SCALE_FACTOR=2).
* Object lifetime: PySide6 frees Python wrappers differently; slots on deleted
  objects (the `is_deleted` sites) and lambdas holding widgets.
* Per-call overhead (§3): measured on real paths in M3.
* High-DPI is always on in Qt 6: rounding of fixed pixel sizes (25/26 px rows)
  at 150 % on Windows — screenshots in M3.


## 8. What M2 actually cost (2026-11-07, PySide6-Essentials 6.11.3)

Measured and changed on the A9 server (Ubuntu 22.04, Python 3.10.20,
pyqtgraph 0.14.0). Probes ran against an isolated install
(`pip install --target /tmp/pyside6_site`), the product env was not touched.

1. **`QThread.finished` cannot be reached past a shadowing subclass signal.**
   `QtCore.QThread.finished.__get__(worker, type(worker))` returns the
   SUBCLASS's signal under PySide6 (probed: `sig is worker.finished` → True),
   and the class-level `Signal` object has no `.connect`. The six workers that
   declared a business `finished` (`PlainReadWorker`, `BgComputeWorker`,
   `BatchProcessWorker`, `WsiCorrectionWorker`, `FullFusionWorker`,
   `SegmentMergeWorker`) now declare **`job_finished`**; `worker.finished` is
   the thread's own signal again and `qt.thread_finished(worker)` returns it.
2. **`stateChanged(int)` vs `Qt.Checked`.** `Qt.Checked == 2` is **False**
   under PySide6 while `stateChanged` still delivers the int `2`; every
   comparison goes through `qt.is_checked(state)` now.
3. **`int(<enum/flag>)` raises** (`TypeError`), confirming the M1 helper: the
   remaining 12 sites in tests/scripts use `flag_value()`.
4. **`QWIDGETSIZE_MAX` is not exported** by PySide6 (neither QtWidgets nor
   QtCore); `qt/QtWidgets.py` defines it as QWidget's maximum (16 777 215).
5. **Module moves** resolved in `qt/__init__.py`: `QOpenGLWidget`
   (QtOpenGLWidgets), `QOpenGLFramebufferObject(+Format)`
   (QtOpenGL), `QShortcut`/`QActionGroup`/`QAction` (QtGui).
6. **`libxcb-cursor0`** (Qt ≥ 6.5 xcb plugin). Without it the app dies at
   "could not load the Qt platform plugin xcb". It is a system package on
   Ubuntu (`apt install libxcb-cursor0`); the isolated verification env used
   a conda-forge `xcb-util-cursor` build through `LD_LIBRARY_PATH`.
7. **sip → shiboken6**: `is_deleted` = `not shiboken6.isValid`, `delete_cpp` =
   `shiboken6.delete`; `sip.setdestroyonexit(False)` has no equivalent and
   `keep_wrappers_at_exit()` is a documented no-op.
8. **Exit crashes in two test modules** (process aborts AFTER the tests pass,
   `viewer/scheduler.py` workers alive at interpreter shutdown). Not seen in
   the product window's open/close path. Open item, see §9.

## 9. Open items from M2

* The test-session exit crash above (2 modules). The product's own open/close
  was clean; the remaining work is to shut the scheduler workers down in those
  fixtures before the interpreter does it.
* Windows has not been run (the user runs it): see
  docs/v17_windows_acceptance.md.
* The paint cost of the Regions overlay is ~30–40 % higher on PySide6 than on
  PyQt5 for the same data (measured with scripts/benchmark_regions_overlay.py);
  the optimisation still wins over the original implementation in every
  scenario.
