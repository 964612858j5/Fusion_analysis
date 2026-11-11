# Step0 background correction on the Mac's GPU (v17 backlog item 5)

Status: plan v1.1, 2026-11-11 (codex round 1 FAIL, two P2 taken; round 2 PASS, §6). Awaiting the user's approval to implement. No product code
changed. User rulings (TG 2026-11-11):

* Option A (optimise first): done in the scratchpad, see §2.
* Both TopHat and the Gaussian ("cuCIM") run on the Mac's GPU.
* Step0's parameter preview must stay as close to immediate as possible:
  the picture follows Enter.
* The tests must show that background correction works and responds in
  real time in Step0's full-image view and in the three-image compare.

Discussed with codex astra high first (`codex_mps_disc.txt`): its points are
taken in §3-§5.

## 1. Today (read-only)

* `core/bg_correction.py` has two backends.
  * **CUDA** (`GPU_MORPH_AVAILABLE`, CuPy): TopHat with a SQUARE footprint
    (`core/gpu_kernels.py`); Gaussian via cupyx or the runtime kernels.
  * **CPU**: TopHat = OpenCV erode + dilate with skimage `disk(r)` and
    `BORDER_REFLECT`, bitwise equal to skimage. The Gaussian is OpenCV
    (`opencv-gaussian`) or scipy (`scipy-gaussian`), which agree to about
    2e-4.
  * A Mac has no CUDA, so **both methods run on the CPU there.** The name
    "cuCIM" is historical: on every machine the method is a Gaussian
    background subtraction.
* **Save** (`ui/step0/search_ctrl.py`, `WsiCorrectionWorker`):
  * Backend choice: one backend per channel, `compute_path(method)` →
    "gpu" | "cpu". A GPU failure raises `GpuBackendFailed`, and the channel
    is recomputed on the CPU from its first tile.
  * Provenance in the channel attrs: `bg_compute_path`,
    `tophat_footprint`, `bg_compute_impl`. Incremental Save compares the
    6-field signature (`normalize_save_signature`).
  * Tiles: 4096, or 1024 for the OpenCV Gaussian (`correction_tile_size`).
    On the CPU they run on `choose_workers(...)` threads; on the GPU, one
    at a time.
* **Preview**: Step0 full image (`ui/step0/step0_explore_tab.py`) and the
  three-image compare (`ui/step0/compare_strip.py`).
  * Each has a `TileScheduler` with `compute_workers = 4` threads.
  * They call `viewer/correction_compute.py` → `_apply_tophat_gpu_or_cpu`
    / `_apply_cucim_or_cpu`, per 512-px tile plus its halo, and for the
    coarse floor (at most 4 MP).
  * The cache key `CorrectionKey` holds the method, the effective
    parameters, the quality level and `BG_CORRECTION_ALGO_VERSION`, but
    not the backend.
* **Free memory on a Mac**: `core/bg_parallel.mem_available_bytes()` reads
  `/proc/meminfo` only, so a Mac gets None. The CPU Save therefore runs at
  most 4 workers on this 12-core Mac, not 6.

## 2. What was measured (Apple M6, 16 GB; the user's slide, one channel 15437×16215; scratchpad `mps/`)

The prototype uses PyTorch MPS (torch 2.5.1, already in the Mac install for
Cellpose).

* **TopHat**, exact disk: the disk is the union of its "staircase"
  rectangles (10 for r=15, 31 for r=50). Each step is one `max_pool2d`
  (min = −max(−x)) over symmetric padding (= `BORDER_REFLECT`). Its result
  equals the OpenCV CPU TopHat **bit for bit** (float32 bit patterns, real
  tissue, r=15 and r=50).
* **Gaussian**: two 1-D `conv2d` passes with the same truncate-4 weights.
  Max difference vs OpenCV: 2e-4 on 0-255 data.

| Whole channel (Save) | product today (CPU, 4 workers) | CPU, 6 workers | MPS |
|---|---|---|---|
| TopHat r=15 (default) | 3.2 s | 2.4 s | 2.2 s |
| TopHat r=50 | 37 s | 30 s | ~15 s |
| Gaussian σ=50 (default) | 4.4 s | — | 1.4 s |

| Preview, after Enter | CPU | MPS |
|---|---|---|
| one screen, 12 tiles, TopHat 15 | 49 ms (4 threads) | 47 ms |
| one screen, TopHat 50 | 856 ms | 478 ms |
| one screen, Gaussian 50 | 107 ms | 42 ms |
| coarse floor (4 MP), TopHat 15 | 160 ms | 35 ms |
| coarse floor, TopHat 50 | 1.7 s | 0.25 s |
| coarse floor, Gaussian 50 | 197 ms | 13 ms |

Phase profile of one 4216² TopHat tile: upload 2 ms, erosion 115 ms,
dilation 115 ms, download 3 ms. The time is compute, not transfer.

## 3. Design

### 3.1 One more backend, "mps", inside `core/bg_correction.py`

* New module `core/mps_kernels.py`, the counterpart of `core/gpu_kernels.py`.
  It holds:
  * `available()`;
  * `tophat(arr32, r)`, the exact disk as in §2;
  * the rectangle decomposition is faster for small radii, while the
    row-by-row running min/max (`r1.py`, `morph_pool`) was faster at r=50
    (0.82 s against 1.05 s per 4216² tile). Both are exact, so the kernel
    picks the faster one by radius; the threshold is measured and written
    down, and tests 1 cover both;
  * `gaussian_bg_subtract(arr32, sigma)`, with the same truncate-4 weights
    as `core/gpu_kernels.py`;
  * **Padding by index, any size.** Both kernels pad by gathering with
    scipy's `reflect` index rule (`i<0 → -1-i; i %= 2n; i = min(i, 2n-1-i)`,
    the rule `core/gpu_kernels.py` uses), not by reflecting each edge once.
    So a 100-px-wide ROI with σ=50 (401-px kernel) works exactly like a
    large one, and so does `BORDER_REFLECT` for OpenCV at any size;
  * one process-wide lock (`_MPS_LOCK`): every MPS call holds it, so
    preview threads and the Save thread never use the device at the same
    time.
  * torch is imported lazily, on the first call or in the smoke test,
    never at module import.
* **When MPS is used.** On no CUDA (`GPU_MORPH_AVAILABLE` false) **and**
  `torch.backends.mps.is_available()` **and** a smoke test passed: a small
  TopHat equal bit for bit to `_tophat_cpu`, and a Gaussian within 1e-3 of
  OpenCV. A CUDA machine is unchanged: it keeps CuPy and its square
  footprint.
* **Readiness never blocks.** `mps_kernels.state()` returns "unknown",
  "ready" or "off" from a flag. It imports nothing, runs nothing and never
  takes the lock. `compute_path`, `current_compute_signature` and the GUI's
  signature checks (`step0_page.py:9231-9253`) read only that flag:
  "unknown" or "off" counts as the CPU.
  * The smoke test (torch import and device start-up) runs once on a
    background thread, started when the Step0 page is built
    (`ui/step0/step0_page.py`).
  * The Save worker runs it itself, on its own thread, if it has not run
    yet before it chooses the channel's backend.
  * Callers waiting on the lock while the smoke test fails get the CPU
    answer.
* **TopHat input rule.**
  * MPS takes an array only when it is 2-D, finite and `min(shape) >= 2r+1`
    (OpenCV's own condition, minus "OpenCV installed").
  * On exactly those inputs, OpenCV and skimage are bitwise equal (block CS
    P1), so MPS equals the CPU whichever of the two the CPU would use.
  * Any other array (non-finite, or smaller than the kernel) is computed by
    `_tophat_cpu` itself.
  * Hence **a TopHat result on "mps" is the "cpu" result bit for bit, tile
    by tile**, and a channel recorded "mps" holds the same numbers a "cpu"
    channel would. The record names the backend rule, not every tile.
* **Gaussian input rule.**
  * MPS takes a 2-D finite array of any size (index padding).
  * A non-finite array is not MPS's to take: in Save it raises
    `MpsCannotTake`, a subclass of `GpuBackendFailed` that does **not**
    disable MPS. The existing branch then discards the channel and
    recomputes it on the CPU from its first tile. OpenCV in turn hands
    non-finite tiles to scipy through the existing `CpuImplUnavailable`
    path.
  * In a per-call (preview) use, the array goes to the CPU.
  * A Gaussian channel is therefore never half MPS, half CPU.
* **Session fallback.** An MPS failure (an exception or out-of-memory)
  disables MPS for the session, like `_disable_gpu_morph`, and the call is
  answered on the CPU.
* **Per-call functions.**
  * `_apply_tophat_gpu_or_cpu`: CUDA first, then MPS, then CPU.
  * `_apply_cucim_or_cpu(prefer_gpu=...)`: `prefer_gpu=False` keeps meaning
    "no GPU". Its callers that pass the CUDA-only flag `CUCIM_AVAILABLE`
    (`core/io_loader.py:346`; the legacy `search_ctrl.py:1636, 1787, 1910`)
    therefore stay on the CPU on a Mac, unchanged.
  * The preview (`viewer/correction_compute.py:124`, `prefer_gpu=True`)
    gets MPS: its 512-px tiles, NATIVE level-0 regions and the floor.

### 3.2 Save

* `compute_path(method)` → "gpu" (CUDA) | "mps" | "cpu". "mps" is chosen for
  both methods when §3.1 holds.
* `tophat_footprint`: "disk" for both "mps" and "cpu".
* `compute_impl`:
  * Gaussian on "mps" → `torch-mps-gaussian`, a new id. It is never mixed
    with `opencv-gaussian` or `scipy-gaussian`.
  * TopHat: None, as for every TopHat.
* `correct_tile(..., path="mps")` calls the MPS kernels. A failure raises
  `GpuBackendFailed` (the existing class, so the existing "recompute the
  channel on the CPU from its first tile" path handles it) and disables MPS
  for the session.
* Every place that reads `path == "gpu"` as "the GPU, one tile at a time"
  is extended to "mps" (search_ctrl.py ~2203 workers, the provenance attrs
  ~2588, `normalize_save_signature`'s legacy rule stays). Found by search;
  the review checks the list.
* Workers: one tile at a time on "mps", as on "gpu".
* **Tile size on "mps": 2048.**
  * The Save holds the lock for one tile at a time, so a smaller tile leaves
    gaps for the preview. Measured alone: about 35 ms per tile for TopHat 15,
    about 250 ms for TopHat 50.
  * This is a design choice, **not a promised bound** on the preview's wait:
    that is measured in §5, item 8.
  * The result does not depend on the tile size: the halo covers the
    kernel, as `correction_tile_size` already relies on.
  * `correction_tile_size(method, impl, path)` takes the frozen path. Both
    callers pass it (`search_ctrl.py:2467` and the per-channel loop at
    `:2504`, including the retries after `GpuBackendFailed` /
    `CpuImplUnavailable`), because a CPU TopHat and an MPS TopHat both have
    `impl=None`.
* **Signature.** A channel saved on the CPU before this change has path
  "cpu", so its signature differs from "mps": the next incremental Save
  recomputes it once. That is the simple, traceable choice codex
  recommended, and no CPU = MPS equivalence rule is added. The user sees
  those channels recomputed once.

### 3.3 Preview

* No new scheduler and no priority queue (AGENTS.md rule 4). The preview's
  4 compute threads call the same per-call functions; under the lock they
  take turns on the device, which runs one kernel at a time anyway.
* No bound is promised. The preview's work is its 512-px tiles, NATIVE
  level-0 regions of about 1024² plus halo, and whole floors. The time from
  Enter to the picture, alone and while a Save runs, is measured in §5,
  item 8.
* If it fails the user's check, the options go to the user. Each is a
  change of this plan, not something added along the way:
  * a smaller Save tile;
  * Save on the CPU while Step0 is open;
  * a priority rule, which would need its own authorisation under AGENTS.md
    rule 4.
* `CorrectionKey` is unchanged.
  * TopHat on MPS equals the CPU bit for bit.
  * A Gaussian preview tile from MPS and one from the CPU (after a session
    fallback) differ by ≤2e-4 of 255, which is display only and invisible.
  * The SAVED tier keeps its own identity check, unchanged.
* The first MPS use pays torch's import and device start-up (measured
  separately in acceptance). The smoke test of §3.1 runs on a background
  thread, so that cost stays out of the first Enter. Until it passes,
  calls use the CPU.

### 3.4 Free memory on a Mac

* `mem_available_bytes()`: `/proc/meminfo` as today. Where that is missing,
  it falls back to `psutil.virtual_memory().available`; psutil is already
  pinned in `envs/ci/requirements-cpu.txt`. None remains the answer only if
  both fail.
* Effect: the CPU Save on a Mac uses `min(cpu//2, memory rule)` workers,
  that is 6 on this Mac. This matters when MPS is off or has failed.
* The same function also sizes Step0's whole-slide fusion workers
  (`ui/step0/overview_panel.py:551`, after its reserve). On a Mac that will
  now follow the memory rule instead of the cap of 4. Linux is unchanged.
  A test checks that worker count with the reserve and a given free
  memory.

## 4. Files (closed list)

* New: `core/mps_kernels.py`.
* `core/bg_correction.py`: the MPS backend in the availability flags,
  `compute_path`, `tophat_footprint`, `compute_impl`, `correct_tile` and the
  two per-call functions; one constant `GAUSSIAN_IMPL_MPS`.
* `core/bg_parallel.py`: `mem_available_bytes` only.
* `ui/step0/search_ctrl.py`, in the Save worker only:
  * the "gpu" checks extended to "mps";
  * `correction_tile_size` gets the path argument, and both callers pass
    it;
  * the smoke test runs before the backend is chosen;
  * `MpsCannotTake` is handled by the existing `GpuBackendFailed` branch.
* `ui/step0/step0_page.py`: only the start of the background smoke test
  when the page is built.
* Tests (new): `tests/test_mps_correction.py`.
* Docs: this plan, `docs/user_guide.md` / `docs/用户指南.md` (one line:
  Macs use their GPU for background correction).

Not changed: the CUDA path, the CPU kernels, the preview scheduler and its
cache keys, the correction plan tiers, the UI.

## 5. Tests and acceptance

Automated (synthetic data only; MPS tests skip where MPS is absent):

1. **TopHat, bit for bit.**
   * MPS vs `_tophat_cpu`, float32 bit patterns.
   * Radii 1, 2, 3, 15, 50 and 150, with both decompositions; non-square
     arrays; edges; an array just at `2r+1` and one below it (which goes to
     the CPU).
   * Integer-valued data and random float data; arrays with NaN and Inf
     (which go to the CPU).
   * The comparison is also made with OpenCV hidden (`_cv2 = None`), so
     the CPU side is skimage.
2. **Gaussian within 1e-3 of OpenCV.**
   * σ 1, 20, 50, 200; tiled vs untiled on MPS equal (halo rule).
   * Narrow and tiny arrays (100×5000 and 7×7 with σ 50) against OpenCV
     `BORDER_REFLECT`.
   * A non-finite tile in a Save raises `MpsCannotTake`; the channel is
     recomputed on the CPU and MPS stays on.
3. **Save on "mps".**
   * A synthetic channel saved tile by tile equals the untiled MPS result,
     seams included.
   * The attrs say `mps` / `disk` / `torch-mps-gaussian`.
   * A forced MPS failure mid-channel recomputes the channel on the CPU
     and records "cpu".
4. **Backend choice.**
   * CUDA present → "gpu".
   * No CUDA and MPS ready → "mps".
   * MPS absent, the smoke test not yet run or failed → "cpu", and
     `compute_path` neither imports torch nor waits on the lock while the
     smoke test runs.
   * `correction_tile_size` with the path is 2048 on "mps" and unchanged
     elsewhere.
5. **Free memory.** `mem_available_bytes` uses psutil when `/proc/meminfo`
   is missing (monkeypatched open).
6. **Regression.** The bg-correction, Step0, compare and Save test files
   against HEAD, one process per test as usual on the Mac.

Measured on the Mac with the user's slide, before vs after, written into
this plan:

7. **Whole-channel Save** for TopHat 15, TopHat 50 and Gaussian 50.
8. **Preview: from Enter to the visible picture**, measured on the real
   window, before vs after:
   * Step0 full image and the three-image compare.
   * Zoomed in (exact tiles), at the NATIVE level and zoomed out (floor).
   * TopHat 15 → 30 → 50 and Gaussian 50 → 20, with Enter pressed
     repeatedly.
   * The first Enter after start (cold) and later ones (warm).
   * Each alone, and while a Save runs.
   * Taken from the existing `perf_trace` marks where they cover the time
     until the tiles are painted; otherwise by an instrumented run in the
     scratchpad. The numbers are written here.
   * "After" must not be slower than "before" in any case.

User's real-machine check:

9. Step0 full image and three-image compare:
   * TopHat and cuCIM are correct and change immediately on Enter;
   * zoom and pan stay smooth;
   * the same is true while a Save runs.
10. **A Save.** The record says mps, and an incremental Save recomputes the
    old CPU channels once.

Rollback:
* `BLOCK01_BG_MPS=0` makes `mps_kernels.state()` "off", so background
  correction runs on the CPU. It is an environment variable, with no new UI.
* It does not undo §3.4: the Mac's CPU worker count is reverted only by
  reverting the commit.

## 6. Plan review (codex astra low)

Round 1, FAIL. Taken:

* P2: the prototype reflected each edge once, so a narrow ROI with a large
  σ would have failed. Padding is now by index with scipy's reflect rule,
  for any size, and narrow and tiny arrays are tested.
* P2: the claimed bound on the preview's wait was not measured. It is
  withdrawn (§3.2, §3.3). Acceptance item 8 now measures Enter to the
  visible picture: cold and warm, at the exact, NATIVE and floor levels, and
  during a Save. If it fails, the options go to the user, and no priority
  rule is added on the way.
* Advisory, taken:
  * `correction_tile_size` takes the path, at both callers and in the
    retries.
  * The input rules are complete: missing OpenCV, non-finite arrays, and
    `MpsCannotTake` for the Gaussian.
  * Callers that pass `prefer_gpu=CUCIM_AVAILABLE` stay on the CPU.
  * Readiness is a flag that never blocks.
  * The warm-up is placed in `step0_page.py`.
  * The fusion worker count gets a test.
  * The rollback wording is honest about §3.4.

Round 2, PASS. One advisory, noted: the scratchpad prototype still reflects
each edge once. Index padding is an implementation gate, covered by tests
1-2.

## 7. Implementation (uncommitted, 2026-11-11)

Files as §4; nothing outside the list.

**Measured with the product code** (`core/mps_kernels`, the user's slide,
one channel 15437×16215, 2048 tiles with their halo, compute only, Apple M6):

| | CPU today (4 workers) | MPS |
|---|---|---|
| TopHat r=15 | 3.2 s | 2.6 s |
| TopHat r=50 | 37 s | 11.7 s |
| Gaussian σ=50 | 4.4 s | 1.3 s |

Tests: `tests/test_mps_correction.py`, 43, all pass.

**Code review (codex astra low).**
* Round 1 FAIL, P2: a call already waiting for the device ran after a failure
  elsewhere had switched MPS off. Fixed: `_on_device` checks readiness under
  the device lock and switches off while still holding it; test added.
* Round 2 PASS.

**Regression** (111 Step0 / correction / compare / Save files, 2765 tests;
failures re-run alone on HEAD 7361b9b): 11 fail only with this change.
* 10 assert the CPU's record or worker count (`bg_compute_path == "cpu"`,
  `opencv-gaussian`, two workers, bitwise equality with the CPU Gaussian);
  on this Mac the Save now records `mps`. All 10 pass with `BLOCK01_BG_MPS=0`.
* `test_step0_channel_conditioning::test_unloaded_channel_lazy_loads_on_switch`:
  timing, about 1 in 4 runs with MPS on (the page's eager DAPI read lands
  after the test clears its counter while the warm-up imports torch); passes
  with MPS off and on HEAD.
* `test_step0_compare_tiles::test_the_dapi_mapping_is_its_own_channels`
  fails on HEAD alone as well (not this change).

Proposed, not done (outside §4): `tests/conftest.py` sets
`BLOCK01_BG_MPS=0` for every test except `test_mps_correction.py` unless the
environment names it, as it already does for `BLOCK01_STEP1_BACKEND`.
