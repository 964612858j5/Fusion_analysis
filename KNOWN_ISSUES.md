# Known issues (2026-11-01)

Open problems that are known and accepted for now, so they are not
rediscovered. Each is fixed by its own approved task; remove a line when it
is fixed.

## Product

* Quitting the app sometimes ends with a segmentation fault (exit 139) after
  the window has closed; no data is affected.
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
* GPU background correction needs a CUDA toolkit (CuPy compiles against it);
  without one the CPU is used.

## Tests

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
