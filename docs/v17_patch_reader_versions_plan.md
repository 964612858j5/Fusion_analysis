# v17 Patch reads never block a switch — plan v1.3 (2026-11-02)

v1.1: codex plan review round 1 NOT READY (3 P2, 1 P3) — answered in §6.
v1.2: codex round 2 NOT READY (1 P2, 3 P3) — answered in §7.
v1.3: codex round 3: only a P3 test-fixture whitelist gap, no production
finding — applied (§5, §1.2); treated as READY.

User (TG 2026-11-02): switching dataset / step / saving a correction and
Step1's patch reading are decoupled, a switch never waits for patch reads.
Their proposal: every data version gets its own reader; readers started for
an old version are void, finish on their own and release it; the patches on
screen are asked again at once. Taken out of the small-items block
(`docs/v17_small_review_items_plan.md` §13). Survey (read-only, file:line at
the working tree on top of 62c95ba) below.

User (TG 2026-11-02, "同意批准"): the new mechanisms (the binding snapshot
and the retired-loader set) are AUTHORIZED; implementation starts once the
codex plan review is READY.

## 0. Facts

* ONE `OMETIFFLoader` object is shared: Step0's `self.loader` travels in the
  handoff (`step0_page.py:13050`) and becomes `MainWindow.loader`
  (`main_window.py:3808`). Its corrected binding is changed IN PLACE by:
  every handoff accept (`main_window.py:4084-4085`, BEFORE that function's
  `_stop_all_loaders` at 4211), Step0 Save (`step0_page.py:12499`, 12676 via
  12546 / 12592 / 12663 / 12213), the dataset switch (`step0_page.py:6387`:
  the OLD loader's store cleared), the Save rollback (12209).
* `core/io_loader.py` keeps that binding in three loose attributes
  (`_corrected_zarr_path`, `_corrected_decisions`, `_corrected_store`) plus
  `correction_config`, with no lock. `read_region` reads them several times
  (105-118, `_read_corrected_roi_only` 249). Three races, on every reader
  thread, not only Step1's:
  1. **stale store pinned**: a reader inside the lazy `zarr.open(path A)`
     (107) assigns store A after `set_corrected_zarr_store(B)` set path B /
     store None — the loader then says B and serves A until the next set;
  2. **crash**: the store set to None between 107 and 108/117 or before
     249 → `AttributeError` on `None`;
  3. **torn read**: decisions of one binding with the store of another.
* Readers holding the object on worker threads: Step1 patch loaders
  (`PreviewLoaderThread`, `workers/cellpose_worker.py:64-125`), random patches
  (`ui/step1_presegmentation/random_job.py`, already generation-checked, mw:7571), Step0's preload
  scheduler, overview reads, batch / on-demand / WSI-correction workers, the
  Step1.5 page (tests only). Fusion builds its own sources (safe); preseg /
  cellpose / mesmer build their own loaders.
* Patch-loader results are ALREADY matched by thread identity
  (`_connect_patch_loader` 8030, `_is_current_patch_loader` 8053): a late
  answer from a loader no longer in `_patch_loaders` is dropped. So the 3 s
  waits in `_stop_loader_for` / `_stop_all_loaders` (8225-8263) protect only
  against the shared-object races above and keep "one loader per patch".
* A new `OMETIFFLoader` per version is NOT cheap where it matters: its
  constructor parses the OME header once, but every (loader, thread) pair
  re-opens the TIFF and builds its zarr view (`_thread_zarr` 321-354, "every
  page header in Python, a qptiff: hundreds"); a fresh object per Save would
  pay that again on every reader thread. The slide's pixels never change
  between versions — only the corrected binding does.

## 1. Design

The user's "each version has its own reader", applied to what actually
changes between versions:

1. **The corrected binding becomes one immutable snapshot** (`core/io_loader.py`).
   A small frozen object `_Binding(correction_config, zarr_path, decisions)`
   with its own lazily opened store (opened once under a lock local to that
   snapshot). `set_corrected_zarr_store` and `set_correction_config` build a
   NEW snapshot and replace `self._binding` in one assignment; nothing in an
   old snapshot is ever changed. `read_region` (and `_corrects`,
   `_read_corrected_roi_only`) take `b = self._binding` ONCE at the start
   and use only `b`; `read_region_lowres` and `_channel_is_corrected` too —
   the lowres dispatch captures ONE binding and passes it down to the
   full-resolution read it falls back to (an internal `_read_region(b, …)`
   behind the public `read_region`), so a helper never re-captures.
   `corrected_store_binding()` stays a method and returns one snapshot's
   (path, decisions) pair; `core/saved_correction.saved_store()` uses it
   instead of reading the two attributes separately. A read started before a Save finishes on the version it
   started with; the next read sees the new one. All three races go, for
   every reader (Step0's too), without new loader objects and without
   re-opening the slide. The old attributes stay readable as properties of
   the current snapshot (`corrected_store_binding`, `correction_config`,
   `_corrected_zarr_path`, `_corrected_decisions` — read by main_window /
   preview_source_provider / tests). `saved_correction.register_loader`
   unchanged (still called by the setter).
   The dataset switch already makes a new loader (a new slide) — that part
   of the proposal exists.
2. **Step1 patch loaders are retired, not waited for** (`ui/main_window.py`).
   `_stop_loader_for(idx)` / `_stop_all_loaders()`: `stop()`, disconnect
   `done` / `progress` / `error`, move the thread from `_patch_loaders` to
   `_retired_loaders` (a set), return at once. A thread that has already
   finished is simply dropped, not retired. Its late answers were already
   refused by thread identity; it finishes its current channel on its own
   (a thread in a blocking read cannot be killed safely) and is released on
   its `finished`. `_start_loader_for` therefore never refuses ("still
   stopping…" goes): a new loader for the same patch may run while the
   retired one ends its last read. `_on_patch_loader_finished` first removes
   the thread from `_retired_loaders` (only that thread), THEN returns on the
   existing identity mismatch — it never touches the replacement's pending
   demand, loader channels or failures. After a handoff accept rebuilds the
   patches (`_load_step0_roi_result` ~4211-4218) AND rebinds the viewer
   (`_step1_sync_whole_slide_source("handoff")` ~4296 — a rebind that fails
   puts the patch view back, 3090), the selected patch is asked again at
   once: ONE call `_ensure_channels_cached(self._preview_patch_idx)` placed
   AFTER that rebind (it keeps its whole-slide guard, so it does nothing
   while the whole-slide viewer shows) instead of waiting for the 400 ms
   debounce; the other patches keep the debounce.
3. **Close** keeps its non-blocking retry: the existing loader gate
   (8413-8418) also waits for `_retired_loaders` (the perf-trace sink closes
   after every loader's `job.end`, 8393 comment).

Not changed: Step0's own workers and their waits (`_stop_bg_workers`
5 s on a dataset switch, the preload scheduler, overview reads) — they gain
the race fix of 1. but keep their stop logic; the other deferred waits in
`KNOWN_ISSUES.md`.

**Where the patch path runs.** Step1 normally shows the whole-slide
viewer, and then the old patch path does not load at all
(`_ensure_channels_cached` and the preload debounce return early when
`_step1_whole_slide_active()`, 8963 / 7867). Patch loaders run when the
whole-slide viewer could not open the slide or a rebind failed (the
fallback `restore_legacy`, 2980-2981 / 3090), and before the first Step1
mount exists (3140 answers False then, so a first handoff can schedule the
patch preload, 7870; `_preload_all_patches` has no guard of its own —
unchanged here). So the freeze removed by 2. is seen on those paths; the binding snapshot (1.) matters for every
reader in normal use (Step0's preload / overview / batch readers, random
patches, `saved_store`).

## 2. What the user sees

* Switching step / dataset, saving in Step0, entering Step1 again: no
  freeze of up to 3 s per patch still reading; the patches on screen start
  loading again immediately.
* A patch clicked again while its old read finishes: loads at once.
* On a slow disk, for at most one channel read, an old and a new read of
  the same area run together (more disk work for that moment).
* Correctness: a patch shows the corrected product of the Save that was
  current when its read started; a Save during a read is picked up by the
  re-request that follows the Save (the handoff accept stops and re-asks).

## 3. Risk and fallback

* Risk: code that relied on mutating a reader mid-read — none found
  (survey §0). Two loaders for one patch at once: results already matched
  by thread identity; the retired one's signals are disconnected.
* Memory: a retired thread holds its partial cache until its read ends.
* Overlap: repeated switches can leave several retired readers (also more
  than one per patch) finishing their current channel together — more
  disk work for that moment; no cap is added (not authorized).
* Fallback: revert the commit; no file format or record changes.

## 4. Acceptance

* `core/io_loader.py`, synthetic OME-TIFF + two corrected zarrs A / B:
  a reader blocked inside the corrected open of A (a patched `zarr.open`
  that waits on an event) while `set_corrected_zarr_store(B)` runs →
  that read returns A's pixels, the next read B's, the loader never ends
  up serving A again; clearing the store during a read never raises;
  decisions and store always from the same snapshot.
* `saved_store` and `read_region_lowres` under a concurrent set: always
  one snapshot's path with its own decisions; clearing during a lowres read
  never raises (`os.path.exists(None)` gone).
* The real `PreviewLoaderThread` on a synthetic slide whose corrected
  products A and B have distinguishable pixels, a read held inside A (a
  controlled delay), then a Save / handoff overlap (store set to B, the
  handoff accept path): the patch shown afterwards holds B's pixels; the
  retired thread's late `done` / `progress` / `error` / `finished` change
  nothing; both `_stop_loader_for` and `_stop_all_loaders` return without
  waiting.
* Whole-slide viewer active → accepted handoff → its rebind fails → the
  selected patch shows B's pixels with no further click.
* Patch loaders (synthetic window, a fake loader thread blocked in a read):
  `_stop_all_loaders` returns at once (no wait called); the retired
  thread's late `done` is not applied; a new request for the same patch
  starts at once; the retired entry goes on `finished`; close refuses and
  retries while a retired loader lives, then closes by itself.
* Regression: Step0 Save / handoff / dataset switch, Step1 patches, the
  corrected-read and saved-correction suites, the close tests.
* Real machine (user): normal use (whole-slide viewer) — Step0 Save while
  Step0 / Step1 pictures load, dataset switch, step changes: the right
  corrected picture, nothing slower. The patch fallback has no supported
  switch to force it on the real machine; it is covered by the synthetic
  tests above with real pixels.

## 5. Scope (closed whitelist; needs the user's authorization: a new
## snapshot object and a retired-thread set)

* `core/io_loader.py` (`_Binding`, the two setters, `read_region`,
  `_corrects`, `_read_corrected_roi_only`, the compatibility properties)
* `core/io_loader.py` also `read_region_lowres`, `_channel_is_corrected`;
  `core/saved_correction.py` (`saved_store`)
* `ui/main_window.py` (`__init__` for `_retired_loaders`, `_start_loader_for`'s
  refusal, `_stop_loader_for`, `_stop_all_loaders`,
  `_on_patch_loader_finished`, `closeEvent`'s loader gate,
  `_load_step0_roi_result` — one call to `_ensure_channels_cached` after
  the patch rebuild)
* NEW `tests/test_loader_binding_snapshot.py`, `tests/test_patch_loader_retire.py`
* test fixtures only, close-test cleanup (their permanently busy fake
  loaders are also removed from `_retired_loaders` before the final close):
  `tests/test_block01_tissue_preview_contract.py` (~1548-1575),
  `tests/test_perf_trace.py` (~734-759)
* test fixtures only (their registered loader doubles gain
  `corrected_store_binding()`): `tests/test_correction_plan.py` (~159),
  `tests/test_step0_engine_mount.py` (~73), `tests/test_step0_compare_engine.py`
  (~391)
* `KNOWN_ISSUES.md` (the patch-loader wait line removed), this plan

## 6. Codex plan review round 1 — answers

1. P2 binding readers missed → `saved_store` via the atomic
   `corrected_store_binding()`; `read_region_lowres` / `_channel_is_corrected`
   capture one binding and pass it down (§1.1, §5).
2. P2 acceptance did not exercise patch readers → a real
   `PreviewLoaderThread` with A/B pixels and a held read (§4); where the
   patch path runs is now stated (§1, before §2), the real-machine check
   says the fallback is covered synthetically.
3. P2 advisory, Step0 preload cache may admit a pre-Save read after Save →
   NOT in this block (no visible failure found; scheduler changes not
   authorized); recorded in `KNOWN_ISSUES.md` with the fix idea (reject at
   cache admission by binding snapshot identity), for the user to decide.
4. P3 → the selected patch is asked again at once after a handoff (§1.2);
   overlapping retired readers documented (§3); `_on_patch_loader_finished`
   order specified (§1.2); paths corrected: `workers/preload_scheduler.py`,
   `ui/step1_presegmentation/random_job.py`.

## 7. Codex plan review round 2 — answers

1. P2 the immediate re-request came before the viewer rebind → placed after
   `_step1_sync_whole_slide_source("handoff")`, with the rebind-failure
   acceptance (§1.2, §4).
2. P3 the three test doubles → listed in §5 (fixtures only; no production
   attribute fallback).
3. P3 the Step0 preload admission note → now actually in `KNOWN_ISSUES.md`.
4. P3 coverage wording → corrected (before the first mount the patch path
   runs too; preload policy unchanged).

## 8. Codex code review round 1 — answers

* P2 display acceptance → the real-thread test now records what the patch
  view is asked to draw: after the Save the view draws B, and the retired
  A read finishing later neither changes the cache nor makes it redraw. The
  accept-path test keeps the real `_load_step0_roi_result` and checks that
  the selected patch is asked AFTER a failing rebind with the patch view
  back. Pixels on the real screen remain the user's check (the patch view
  shows only on the fallback, §1).
* P3 tests that did not fail on the old code → rewritten so each one fails
  on 4e7187d by the old race itself: stale A after B (A served again), the
  store cleared between open and use (`TypeError: argument of type
  'NoneType'`), the lowres dispatch and its read on different versions
  (raw pixels for a corrected channel), `saved_store` returning A's path
  with B's decisions. (Two tests are structural — they read `_binding` /
  `_retired_loaders` — and fail on 4e7187d for that reason, not by a race.)
  The display checks establish redraw requests carrying B, not rendered
  pixels.

## 9. Code review round 2: PASS — and one change after it

The regression comparison showed three handoff-contract tests counting
an extra `_ensure_channels_cached` on every handoff (a reload that must
cost nothing). The immediate ask is now made ONLY when the whole-slide
viewer was showing before the rebind and is not after it (the failed
rebind of §7.1); every other case is already asked by the existing paths.
Test: a working rebind asks nothing extra; a failing one asks after it.

Test-only exception (found by running the file 3x on both trees): two
tests in `tests/test_step1_overlay_preview.py` swapped in a held loader
while the window's own first read could still be on the patch's slot; the
old 3 s stop-wait made that read finish by luck of timing. They now wait
for that first read to end before swapping. Result over 4 full runs: only
the pre-existing `test_a_mapping_change_drops_only_that_channels_pixels`
fails, as on 4e7187d.
