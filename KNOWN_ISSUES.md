# Known issues (2026-11-01)

Open problems that are known and accepted for now, so they are not
rediscovered. Each is fixed by its own approved task; remove a line when it
is fixed.

## Product

* Quitting the app sometimes ends with a segmentation fault (exit 139) after
  the window has closed; no data is affected. Not seen on Ubuntu 22.04
  (PySide6 6.11.3, RTX 2080 Ti, X11): 3 of 3 closes exit 0 (backlog 7,
  2026-11-12).
* Loading a slide freezes the window for about 4.8 s (deferred by the user).
* Step0: the wheel zoom has no upper limit.
* Step0 compare on a spinning disk: a channel switch takes ~1.1 s cold
  (SSD ~0.65 s); the cost is seeks (~90 scattered blocks per switch), not
  read-ahead (measured, `docs/v17_step0_compare_reads_plan.md` §6). Set
  aside by the user.
* Step4 memory at large cell counts: the obs table, the per-label geometry
  (~152 B per label) and finalization temporaries are held whole (X and the
  CSV are streamed). See `docs/step4_20m_laptop_plan.md` (draft).
* Step1: a drag occasionally stalls.
* Step3: drawing a mask costs about 50 ms per frame.
* Step1 / 2 / 3 tissue snapshots drop channels whose shape does not match.
* Some Step2 refusal messages still mention TMA where the slide has no cores.
* On a CPU with AVX512, NumPy's float32 power differs by 1 ulp, so the Python
  fusion fallback can differ from the Rust fusion by 1 grey level in some
  pixels (suspected from CI, not yet confirmed).
* Linux with glibc 2.28-2.33 (RHEL/Rocky/Alma 8, Debian 10/11, Ubuntu
  20.04) gets PySide6 6.9.3 instead of 6.11.3: PyPI's 6.10+ wheels need
  glibc 2.34 (docs/v17_older_linux_qt_plan.md). The code must keep running
  on 6.9.3 (the check is in `envs/fusion_mesmer/README.md`). Below 2.28 the
  installer refuses. On RHEL/Rocky 8 `libxcb-cursor.so.0` comes from EPEL
  (`xcb-util-cursor`). Accepted by the user on Linux 2026-11-11.
* GPU background correction needs a CUDA toolkit (CuPy compiles against it);
  without one the CPU is used.
* Waits still blocking the window (`docs/v17_small_review_items_plan.md`
  §3, deferred): Step0's release of a finished background process; the pre-segmentation job's close on a
  dataset switch; the montage's close; stopping background workers when a
  dataset is loaded.
* Step0's preload cache can admit a read that started before a Save after
  the Save dropped that channel (it checks only the dataset generation,
  `workers/preload_scheduler.py` ~385). No visible failure found (the
  preload result does not repaint; Intensity uses the whole-slide
  provider). Fix idea: reject at admission by the loader's binding snapshot
  (`docs/v17_patch_reader_versions_plan.md` §6.3); not authorized yet.

## Tests

* `test_step1_shared_camera.py` and `test_step1_gpu_takeover.py` crashed at
  interpreter exit, after all tests passed, on the Linux A9 server with the
  viewer core and GL (docs/v17_pyside6_plan.md §8; root cause not
  re-confirmed). On macOS (no viewer core, offscreen) both exit 0: 15 passed,
  and 2 passed / 45 skipped. Not reproduced on Ubuntu 22.04 on a real X11
  desktop with the viewer core (PySide6 6.11.3, 2026-11-12): 3 runs each,
  all exit 0 (15 passed; 13 passed / 34 skipped -- the skipped ones need
  `BLOCK01_REQUIRE_STEP1_GPU=1` and were not run).
* macOS, PySide6 6.11.3: the root conftest's between-test `gc` sometimes
  segfaults inside shiboken6 (seen on unchanged HEAD); run one test per
  process there. `test_install_scripts.py` needs Linux tools (uname, flock,
  `date -Is`, bash >= 4): 10 cases fail on macOS, also on HEAD.

* Known failing modules (also left out of CI, `tests/ci_cpu_modules.txt`):
  shared intensity lifecycle, real load first frame, preview source provider,
  Step0 compare channel state, correction parameter inheritance, intensity
  window, source-aware conditioning, Step0→Step1 hand-over contract, compare
  tiles, Step1 channel state contract, Step1 live controls, Step1 overlay
  preview.
* `test_step0_channel_conditioning.py`: a lazy-load count races a background
  read on a slow machine (reproduced on one core), and once hung on a GitHub
  runner.
* `test_step0_engine_mount.py` and a Step0 test are flaky under parallel
  load; reparenting Step0's GL host to another window segfaults in tests;
  `test_v16_a6_workspace` dumps core at exit; `step1_5_bg_page` prints a
  resizeEvent error.
* Several GUI test modules need a real display (`DISPLAY=:1`); offscreen they
  abort at collection.
