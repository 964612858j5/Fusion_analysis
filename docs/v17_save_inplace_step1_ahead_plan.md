# Step0 Save in place + Step1 prepared ahead (v17)

Status: plan v1.3 (Part A implemented), 2026-11-02 (v1.0 + astra low review round 1, §6). User rulings of the same day (Telegram).
Not started. The patch-reader change (`docs/v17_patch_reader_versions_plan.md`)
and the compare-mode chrome removal stay uncommitted until this lands and
passes the real-machine check.

## 0. What the user saw (perf log `/root/block01_perf.log`, TMA1, HDD)

* Save looked stuck before it began: 33 s copying the previous correct run
  (Vimentin, 3 GB) into a new run folder (`_rm_copy_pixels`), then 31 s
  computing the one changed channel (VISTA).
* Entering Step1: 1.2 s GUI block on the first entry (the Rust mount is built
  then: `step1.entry.mount_open` 433 ms, `switch.rm_save_view` 570 ms), then
  ~2 s of coarse-then-fine. A rebind after Save tears every channel down,
  including the unchanged ones (DAPI raw).

## 1. User rulings

1. **Save overwrites the workspace's correct product in place.** No new correct
   run per Save and no copy of unchanged channels. Only "Save as new workspace"
   (existing, `_save_as_new_workspace`) makes another.
2. **Downstream results are not made stale by a correction change.** A
   segmentation is out of date only when the user changed Step1's fusion
   settings and segmented again. No notice either, even when the changed channel
   has fusion weight > 0 (user, 2026-11-02).
3. **Step1 is prepared in the background once Step0 has loaded**, whether or
   not the user saves. Yield to Step0's own disk reads; follow the camera ~0.5 s
   after it stops; at most **15 channels**.
4. A shared Step0/Step1 tile cache is NOT in scope (later, with the UI rework).

## 2. Part A — Save in place

### A1. Which product is "the current one"
`_rm_base_corrected_path` already resolves the run the published handoff
names. New rule: that run is THE product of the workspace and Save writes it.
When the handoff names nothing usable (first Save, or the run is missing as on
TMA1 now) → the newest `is_done` correct run of the workspace, else a new run
(the only case `run_store.new_run(ws, "correct")` remains).

### A2. Writing one channel without a window where it is half there
`WsiCorrectionWorker` already merges into an existing zarr (`incremental=True`,
mode "a"), but today it deletes the channel's dataset first and writes tiles
into the live name, so a crash or a concurrent reader sees a partial channel.
Change, per channel being recomputed:

1. write into `<group>/<ch>.__new__` (dataset), its coarse plane and display
   layers into the matching `.__new__` names;
2. when the channel is complete: rename on disk `<ch>` → `<ch>.__old__`,
   `<ch>.__new__` → `<ch>` (same for the coarse plane and display layers),
   then delete `.__old__`;
3. cancel / error: delete `.__new__`; the old channel is untouched;
4. startup / open: leftover `.__new__` deleted, a leftover `.__old__` with no
   `<ch>` renamed back (crash between the two renames).

Extra disk: one channel's product while it is being written (~1–1.5 GB on
TMA1), freed at the swap.

### A3. Readers during the swap
An opened zarr directory store is not a snapshot, so the swap is safe only
when nothing reads the product. Before the first swap of a Save (not before
the compute — the compute writes `.__new__` only):
* every reader of the corrected product is released and new reads held, in
  the existing release/rebind lifecycle: Step0 (already), Step1, Step3 and
  Regions viewers, running patch loaders incl. retired ones (wait for their
  `finished`), the tissue preview's saved-stage readers;
* after the swap the viewers rebind (`handoff_revision` moves; the Step1
  identity already folds each channel's `source_identity`,
  `viewer/step1_source.py:273`).
* Patch reader: the per-version binding (§ patch-reader plan) holds an opened
  store; after the swap the next binding re-opens it.
* **Jobs that read the product**: Save is refused while one runs on THIS
  workspace (the job's captured input, not the displayed workspace) —
  "A segmentation is reading these channels; Save when it has finished."
  Reuses `MainWindow._busy_reason` (`_fusion_worker` + retired ones,
  `_step2._worker`, `_step4._worker`, `_preseg_job`). Regions detection reads
  the raw slide only and is not a reason.

### A3b. Cancel, error and region change
* Channels are swapped one by one as each completes. Cancel or error after
  channel A was swapped and before B: A stays new (it is complete and its
  record is written), B keeps its old pixels; the settings and `params.json`
  record exactly that mix (no "restore previous settings" over A). Today's
  `_on_wsi_canceled` deletes the whole zarr — removed.
* A changed analysis region (`incremental=False`) is the one case that
  rewrites every channel: written as a complete product into a sibling
  `corrected_channels.__new__.zarr` (+ sidecars) and swapped as a whole when
  done; cancel deletes the sibling. Same reader release as A3.
* Recovery on open covers: `.__new__` alone (delete), `.__old__` alone
  (rename back), live + `.__old__` (delete `.__old__`). Token-mismatched
  coarse/display planes stay rejected as today.

### A4. Records
* `params.json` of the run: rewritten atomically after each Save (summary,
  `corrected` signatures, `created_at` kept, new `updated_at`). Publication
  and corrected-artifact registration today run only for a pending new run
  (`step0_page.py` `_emit_complete`, `_rm_pending_run`): they run for an
  in-place Save too. The tissue cache's freshness (product/`.done`/params
  mtimes, `main_window.py:3227`) then sees the change.
* Provenance: the per-channel artifact records already carry the signature;
  a Save registers the new channel artifacts again (same as today).
* Downstream runs keep pointing at the same correct run (`inputs.json`
  `upstream`); nothing is marked stale (ruling 2). This needs a real change:
  pre-segmentation staleness (`preseg_run.is_stale`, `pixel_identity`)
  includes the corrected channels' decisions and write tokens and today
  shows "out of date" and drops the selection. The correction part leaves
  that check (raw slide, ROI and fusion settings stay); display caches keep
  using the tokens for freshness — pixels on screen are always current.
* **Fusion settings follow their component channels (user ruling,
  2026-11-02).** When a Save changes the correction of a channel that is a
  fusion component (committed weight > 0), the saved fusion settings become
  unsaved — the same path as a weight change: Step1 shows "Unsaved fusion
  changes" and says which channel's correction changed, and Generate is
  refused (`_require_committed_fusion_settings`) until "Save Fusion
  Settings". Mechanism: today's binding check (`_settings_binding` /
  `_rm_check_settings_binding`, block RM option C) drops the snapshot when
  the correct run PATH changes; with in-place Save the path never changes, so
  the binding records instead each component channel's correction identity
  (decision + `source_identity` token) and the check compares those. A
  changed channel with weight 0 changes nothing. The re-saved settings hash
  includes the new identities, so a Generate after it never reuses the old
  fused product (`find_same` sees another reuse key) and pre-segmentation
  made with the old settings shows out of date — consistent with ruling 2
  (stale only after fusion settings are saved again).
* The run is no longer "published = frozen". `run_store.publish` stays the
  `.done` marker of a complete run. Docs that state "a published run is never
  written" (ARCHITECTURE.md, the RM plan) get a dated note.

### A5. Removed
`_rm_copy_pixels`, the `copy`/`defer_copy` path of `_rm_new_correct_run`, and
the copy half of `_rm_prepare_save` (the viewer hand-off half stays).

## 3. Part B — Step1 prepared ahead (v1.1: B2 needs its own Step1 path)

### B1. What exists to reuse
Step0 already prepares compare mode off screen (`_prepare_compare_ahead` →
`CompareEngine.prepare_ahead` → `host.paint_ahead`): after the full image has
been still for `_COMPARE_AHEAD_QUIET_MS`, the cores are made and painted off
screen one per event-loop turn, stopped by any camera move and by production
runs, bounded by `AHEAD_BUDGET_S`. Measured first entry 1.4 s → 65 ms. The
PATTERN is reused; the paint API is not: `gl_host.paint_ahead` takes Step0
panels, and Step1's mount initialises synchronously through
`show()`/`grabFramebuffer()` (`step1_viewer_mount.py:416`). B2 adds a staged
Step1 initialisation (one piece per turn) and an off-screen paint of Step1's
own GL widget at the target viewport size.

### B2. Behaviour
* Trigger: Step0 has a dataset and the full image is still for the quiet time
  (the same timer as compare-ahead, run after it).
* Step 1 (once per dataset): build Step1's mount hidden — the 1.2 s now paid
  on first entry — one piece per event-loop turn, like Fable 5b's split.
* Step 2 (each time Step0's camera rests): set Step1's camera to Step0's (the
  camera Step1 adopts on entry) and paint it off screen until the visible
  channels (Step1's ticked channels, at most 15; more → only the first 15
  ahead) have their tiles for that view.
* Before any Step0 Save (no published handoff yet) only raw channels can be
  prepared; corrected channels come after the first Save.
* Yield: not while Step0 has reads in flight (full image or compare panels:
  a read-idle gate over their schedulers' in-flight counts AND the native
  cores' existing `core.idle()` for the full-image and compare cores),
  not during a production run, stop on any camera move, budget per round.
* GPU memory: Step1's prepared tiles count against Step1's existing budget;
  when the GPU is short, ahead preparation is the first thing dropped.

### B3. After Save: only the changed channels move — DEFERRED
Step1 has ONE native `ViewerCore` for all channels
(`native/fusion_viewer_core/src/viewer.rs:86`) and no per-channel source
replacement; keeping unchanged channels' tiles across a rebind needs a Rust
core change and a new core release. Not in this round: with B2 the rebuilt
view is prepared in the background right after Save while the user is still
on Step0, which covers the user's case. Kept for the next core release
(with the held level check).

## 3b. Files (closed list)
A: `ui/step0/step0_page.py`, `ui/step0/search_ctrl.py`, `utils/run_store.py`
(recovery helper), `ui/main_window.py` (reader release, `_busy_reason` use),
`core/preseg_run.py`, `core/provenance.py` (wording only if needed),
`viewer/display_pyramid.py` (subtree swap helper), ARCHITECTURE.md,
KNOWN_ISSUES.md, tests. B2: `ui/step1_viewer_mount.py`,
`ui/step1_viewer_host.py`, `ui/main_window.py` (trigger/gate),
`ui/step0/step0_page.py` (quiet/idle signal), tests. No native change.

## 4. Tests (synthetic fixtures only)
* A: in-place Save rewrites only the changed channel (others' chunk files
  untouched, same inode/mtime); no new run folder; crash between renames
  recovers; cancel leaves the old channel; Save refused while a Step2 job
  reads the product; leftovers cleaned on open; run_store tests updated.
* B: ahead preparation starts after the quiet time, stops on a move, yields
  while Step0 reads, respects 15 channels, entering Step1 after it has run
  draws without a coarse frame; after Save the whole view is rebuilt and
  prepared again in the background before the user enters Step1.
* Real machine (TMA1 + HDD): Save of one channel = compute time only; enter
  Step1 with and without Save — sharp at once.

## 5. Order
A first (storage and the stuck Save), then B2. Each: astra review at low
effort, tests, real-machine check.

## 6. Review round 1 (astra, low) — CHANGES, all taken
P1 swap not reader-atomic → A3 release list; P1 cancel/geometry paths → A3b;
P1 preseg staleness → A4; P1 B3 needs native → deferred; P2 `_busy_reason`
incl. preseg, job's own workspace → A3; P2 publication for in-place Save →
A4; P2 fusion reuse → A4 (user ruling); P2 Step1 paint path → B1/B2; P3 identity
already folded, display subtree, recovery cases, file list → A2/A3/A3b/§3b.

Round 2 (astra, low): CHANGES — two P2, both taken (native `core.idle()` in
the read-idle gate; B acceptance no longer claims per-channel rebuild).
Rule 7: no product defects left at plan level; reviews of the code follow.

## 7. Settled with the user
Fusion after a correction change: §A4 "Fusion settings follow their
component channels" (user, 2026-11-02).

## 8. Part A as implemented (v1.3)

* One change from A2/A3b: the swap is **at the end of the whole run**, not
  per channel. The worker computes every changed channel into
  `<run>/.staging/` (a product of its own, same layout) and moves them in
  together when the last one is complete (`core/product_swap.py`:
  `swap_in` per channel subtree, coarse and display before level 0;
  `swap_whole` for a new region). Cancel or error deletes the staging: the
  live product and the settings come back exactly as before, as today's
  "unfinished Save" path already promises. Simpler than per-channel mixes.
* Journal `<run>/swap_journal.json` before the first rename; `recover`
  rolls a journaled move forward, deletes staging/old leftovers without
  one. Called at Save start and for each correct run on workspace open.
* Withdrawn channels leave the product only after the Save completed
  (`remove_channels` after `_emit_complete`); an all-withdrawn Save stages an
  empty product and swaps it whole (never "delete first").
* Base product: the run the handoff names, else the newest finished correct
  run of the workspace, else a new run (`_rm_base_corrected_path`).
* Save is refused while `_busy_reason` reports a job (via
  `deletion_blocker`).
* `params.json` is republished by every Save (`created_at` kept,
  `updated_at` new).
* Pre-segmentation staleness leaves the correction part out when both
  sides carry the structured identity (`preseg_run.is_stale`).
* Fusion: committed settings record `bound_to.components` {channel:
  [decision, token]} of the components (weight > 0);
  `_rm_check_settings_binding` / the restore check drop them when one
  changed; Generate's refusal names the channel; the fuse reuse key carries
  the components (a fused product made before this change is not reused
  once — it is generated again).
* Removed: `_rm_copy_pixels`, the copy path of `_rm_prepare_save`,
  `WsiCorrectionWorker._drop_plane` / `_discard_partial`.
* Files beyond §3b: `core/product_swap.py` (new), `tests/test_save_in_place.py`
  (new).

### 8.1 Code review round 1 (astra, low) — CHANGES, all taken

* P1 reader drain: the move is no longer made by the worker. It records a
  `swap_plan`; the page moves the channels in on the GUI thread
  (`_on_wsi_finished` → `_move_into_product`) after the window's
  `before_product_swap` lets the tissue preview's saved-stage reader and the
  patch loaders go (late results refused by key / identity). No read can
  start during the move: it runs on the thread that would start one.
* P1 swap error lost channels / P1 pixels committed before publication:
  the move is now UNDOABLE until the Save has published its handoff.
  Journal entries carry each replaced folder's backup path; `commit` after
  `_emit_complete` succeeded, `rollback` when it did not (and on any
  exception during the move); `recover` on open = rollback of a journaled,
  never-committed move. The all-withdrawn path and withdrawn channels use
  the same move + commit.
* P2 correction alone invalidated preseg: the committed fusion settings
  are KEPT when a component changed; `_fusion_unsaved_why` makes them dirty
  (Generate refused, reason shown) until saved again.
* P2 re-save restored the old hash: a pre-segmentation run records the
  committed settings' component identities (`run["fusion"]["components"]`);
  `is_stale` compares them with the current committed ones, so a re-save
  with the same weights still makes older results stale.

### 8.2 Code review round 2 (astra, low) — CHANGES; three taken, one recorded

* Taken: a rollback re-reads the channels it put back (caches no longer
  keep the rejected pixels) and puts the run's `params.json` back; removing
  withdrawn channels after publication recovers on an error.
* Recorded, not changed (P1 "reader drain incomplete"): the saved-stage
  reader and the patch loaders are let go, not waited for (no GUI-thread
  waits, user ruling); a read still running across the move has its result
  refused (saved-stage key, loader identity), so no moved pixels reach the
  screen through it.
* Rule 7: third round not run; real-machine check next.
