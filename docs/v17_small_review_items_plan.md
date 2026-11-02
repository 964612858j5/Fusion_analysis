# v17 The four small review items — plan v2.5 (2026-11-02)

v2: codex plan review round 1 NOT READY (4 P1, 4 P2, 1 P3) — the risky C9
conversions are taken out (§3 "Deferred"), the rest reworked (§8).
v2.1: codex round 2 NOT READY (1 P1, 2 P2, P3s) — answered in §9.
v2.2: codex round 3 NOT READY (1 P1, 2 P3) — answered in §10.
v2.3: codex round 4 NOT READY (2 P2) — answered in §11.
v2.4: codex round 5 NOT READY (1 P2) — answered in §12.
v2.5 (codex round 6 READY, its two P3 wording fixes applied): user (TG 2026-11-02, "同意"): the patch-loader change is taken OUT of
this block (§3 item 2) and becomes its own plan right after it (§13).
User (TG 2026-11-02): A6 → option A; C10.3 → report, do not refuse.
User (TG 2026-11-02): no core release now → R2 is HELD OUT of this commit
(the installer downloads the release named by Cargo.toml's version; 0.2.2
without a release would break a new install). The Rust change, the 0.2.2
bump and its test stay uncommitted on this server and go in with the next
core release.

User (TG 2026-11-02): "先修复1中的4个小项" — the remaining whole-code-review
items: retire the Python GPU path (Fable A6), GUI-thread blocking waits (C9),
the Rust unchecked level index (R2), swallowed errors on protected paths
(C10). Surveys (read-only, file:line as of HEAD 419c7c1) by two search agents;
facts below re-checked where they decide the change.

## 1. R2 — a level from Python used as an unchecked index (done, uncommitted)

`ViewerCore` methods taking a level from Python (`set_view`,
`view_covered_at`, `complete_python_tile`, `fail_python_tile`,
`cached_tile_for_test`) check it against the pyramid first (`level_ok`,
`src/py/viewer.rs`): ValueError "level N is not one of this slide's M levels".
`gate_tiles` already clamps; the raw reader returns a bounds error; the
corrected sources ignore the level (`step1_tile.rs:240`) — no index there. Test `tests/test_core_level_check.py`. Crate 0.2.1 → **0.2.2**
(the released 0.2.1 must not change); a `core-v0.2.2` release after this
block is the user's decision then.

## 2. A6 — the Python GPU path, option A (user: A)

Full removal is not small (no Rust montage; slides the core refuses and
runtime hand-overs fall back to it; the Rust parity tests use it as reference;
large CI suites drive the mount through it) — it becomes its own plan later,
after a Rust montage and broader Rust decoding. Now:

* NEW `ui/step1_display_types.py`: the objects the Rust / Step3 / CPU code
  imports from the Python GPU modules, MOVED UNCHANGED (same code, no new
  behaviour): from `ui/step1_gpu_layer.py` `MODE_OVERLAY`, `MODE_FUSION`,
  `ROI_POLYGON_BOUNDS_TOLERANCE`, `sanitize_roi_polygon`, `DisplaySnapshot`,
  `ViewportSnapshot`, `LabelPlane`, `LabelLayer`, `LabelSnapshot`,
  `LABEL_OUTLINE`, `LABEL_FILL`, `LABEL_TEXTURE_BYTES`, `MAX_LABEL_RADIUS`,
  `label_radius`, `_LabelTextureLru` (the whole class, with what it uses:
  `_as_name`, `_TextureRecord`, `_check_gl`), `Step1GpuLayerError`;
  from `ui/step1_gpu_binding.py` `BindingBudgets`. Each old module imports
  them back under the same names (identical objects; nothing that imports
  from the old modules changes). If moving one would drag a Python-GPU-only
  dependency along, it stays and is listed in the implementation notes.
* The consumers import from the new module: `ui/step1_core_binding.py`,
  `ui/step1_core_labels.py`, `ui/step3_core_labels.py`,
  `ui/step3_label_binding.py`, `ui/step1_viewer_mount.py`.
* `BLOCK01_STEP1_BACKEND` (`requested_backend`, `ui/step1_core_adapter.py`):
  unset / `rust` → rust; `python` → python (unchanged); any other value →
  rust with one printed warning (it silently meant python).
* `ARCHITECTURE.md`: one line — the three viewer paths, the Python GPU path
  is the fallback for what the core refuses and the montage's picture, and
  its removal waits for a Rust montage. (Historical plans that say the
  default was `python` stay as written: they record their time.)

## 3. C9 — blocking waits on the GUI thread (narrowed)

Only changes whose correctness does not depend on the removed wait:

1. **`MainWindow._stop`** (Stop button, dataset switch; `ui/main_window.py`
   ~9703-9722): the `import torch; torch.cuda.empty_cache()` block deleted
   (the GUI process never holds a CUDA cache; `step2_page` fixed the same
   mistake). `_cleanup_cellpose_process` is NOT changed (codex round 6: the
   GUI process only consumes that queue, it has no feeder thread, so its
   `join_thread()` does not block; nothing to fix).
2. **Patch loaders**: NOT CHANGED in this block (user 2026-11-02, §13).
   `_stop_loader_for` / `_stop_all_loaders` / `closeEvent`'s loader retry
   stay exactly as today; the retired-loader bookkeeping of v2.2-v2.4 is
   withdrawn. The real cause (every patch loader shares one `io_loader`
   whose corrected store Step0 Save rebinds in place) is the subject of the
   separate plan in §13.
3. **Fusion worker**: `closeEvent`'s `worker.wait(3000)` (~8361-8364) dropped
   (the retry right after already handles a live worker);
   `_retire_fusion_worker`'s `wait(2000)` (~11768) → 0 (its survivor list
   exists; callback invalidation exists).
4. **Regions on close** (`closeEvent` → `regions.shutdown()` →
   `ui/tma/workers.shutdown(15000)`): `shutdown(timeout_ms=0)`; while a job is
   live `closeEvent` ignores, shows "Waiting for Regions…" and retries itself
   (`QTimer.singleShot(500, self.close)`, the existing pattern) instead of
   blocking or asking for a second close; the stale "a few seconds at most"
   comment fixed.

**Deferred (codex round 1: not safe as a small change; to `KNOWN_ISSUES.md`)**:
Step0 Process's unbounded release wait (needs a pending-Process state known
to Save / close / Stop and the display contract kept), the pre-segmentation
job's close on dataset switch (its `terminate` path still blocks; close
semantics would change), the montage supply's close (callback generation,
provider lifetime), `_stop_bg_workers` on dataset Load (refuses safely).

## 4. C10 — swallowed errors on protected paths

**Reporting.** `utils/error_hook.py` gains `report(exc, where="")`: the
installed hook's own log (its lock) and notifier, so concurrent reports share
one writer; before `install` (tests, scripts) it writes the log and stderr
only. And `set_closing(on)`: while on, notices are dropped AT DELIVERY
(`Notifier._show` checks it), so a report queued from a worker just before
the close cannot pop up after it; reports are still logged and printed.
`closeEvent` turns it on when the close is accepted and off again whenever
the close is refused / retried (a Regions job, a live loader, the user's
No). `set_closing(True)` also closes a notice window that is already open
(the notifier's box is parentless and could outlive the main window and
hold the application open) and releases a `"pending"` reservation whose
delivery it drops; `set_closing(False)` (the user's No, a refused close)
leaves reporting working as before. Never raises. No other new mechanism.

1. **Regions handoff hash**: `_go_to_regions` checks the handoff BEFORE it
   switches pages (it switches first today, ~5743): an empty handoff path or
   an unreadable file → `_regions_refused(text)` and return, the current page
   and the Regions session untouched. `_regions_context` keeps its dict
   contract for its other callers (~2258, 6703, 6811);
   `core/regions/document.set_session` skips the `handoffs` entry for an
   empty sha, so no path can register a phantom empty-sha input.
2. **Session save on step change / close** (`_rm_save_view` ~8243-8270): each
   of its five writes in its own `try`, so one failure does not skip the
   rest; each failure `report`ed. The inner helpers (`_save_step1_session`,
   `rm_save_draft`, `rm_save_shared_windows`) keep never raising
   (`_on_fusion_done` relies on that) — the parts that today run outside
   their `try` (building `_save_step1_session`'s payload, `rm_save_draft`'s
   session I/O) come inside it — and `report` instead of printing. A
   FAILURE is only an exception they caught; "nothing to write" (no
   workspace, a pending restore, empty windows, unchanged content —
   `run_store` returning False) is not. To keep that apart without changing
   their return values, `_rm_save_view` counts failures through a small
   per-call list the helpers append to (an optional argument). On close, the
   save stays where it is (every close attempt, retries included, saves
   again, so the newest state is written), but the question "The session
   could not be fully saved (…). Close anyway?" is asked only at the FINAL
   acceptance point — after every retry gate (fusion, loaders, overview
   reads, Step2, Step4) has passed, just before `finalize_close` and
   `set_closing()` — using the failures of THIS attempt's save. A gate that
   retries never asks, so the question comes at most once per close the
   user started; Yes closes, No ignores the event and `_display.resume()`s
   (as the other refusals do). No new state.
3. **Step0 published run without slide identity**
   (`_rm_publish_correct_run` ~12241-12256): the identity is UNAVAILABLE when
   `slide_id_of` raises or returns `None`/empty — then the run is still
   published, with `inputs.json` `"slide_id_unavailable": "<reason>"` (new
   optional argument of `run_store.write_inputs`), and a warning tells the
   user how to repair (put the slide / project file back and Save Step0
   again). An unreadable project manifest alone is not that: `slide_id_of`
   can still compute the id from the slide; the id computed is kept and the
   manifest problem is only `report`ed.
4. **Step0 `_rm_note_editing`** (~12322-12324): a `QMessageBox.warning` on
   that Save (a user action).
5. **Intensity lock** (`block01_display.apply_intensity_policy` ~2653-2658):
   `report` instead of `pass` (the `RuntimeError` pass for a deleted widget
   stays).
6. **Provenance registration** (`core/provenance.py` `safe_register` and its
   siblings ~231 / 385 / 438 / 534 / 597; `workers/segment_merge_worker.py`
   ~391 / 596): `report` instead of `print` — still never failing the product.
7. **Smaller protected writes** (`mark_roi_step` main_window ~10417 /
   `core/step0_handoff.py` ~402, pre-seg `.done` `run_job.py` ~141,
   `_recover_interrupted_publish` ~10822, `_rm_note_viewing` ~11509):
   `report` instead of `print`.
8. **Quantification pixel size** (`core/regions/quant_apply._pixel_size`
   ~94-108): the reason is recorded in the Step4 provenance
   (`regions.pixel_size_unknown`) instead of a silent `None`.

The cosmetic / Qt-teardown `except: pass` blocks stay (not counted precisely).

## 5. Acceptance

* R2: `tests/test_core_level_check.py`; `cargo test`.
* A6: the moved names are the identical objects in the old and new modules;
  the Rust / Step3 / CPU suites and the Python GPU suites unchanged and
  passing; `requested_backend` for unset / rust / python / a typo.
* C9: `closeEvent` retries while a Regions job lives and closes by itself
  when it ends; `_stop` imports no
  torch (a sentinel in `sys.modules`); the fusion survivor path keeps its
  invalidation; the single "Close anyway?" of §11; patch-loader behaviour
  unchanged (the existing loader tests pass untouched).
* C10: per site a forced failure on a synthetic project — the real
  `error_hook.report` (installed notifier in the test) logs once and notices
  once; after `set_closing()` no notice; `_rm_save_view` keeps doing the other
  writes after one fails, and a "nothing to write" (no project, pending
  restore, unchanged content) is not a failure; a failing Step1 session write
  at fusion end still unlocks the UI; close with a failed write asks and No
  keeps the window; report → `set_closing(True)` → queued events processed
  → no notice; a refused close turns it off again;
  `_go_to_regions` refuses with an unreadable handoff before switching (the
  page and session unchanged) and `set_session` with an empty sha adds no
  `handoffs` entry; a Step0 publish without identity records the reason.
* Regression: step change, dataset switch, Step0 save / publish, Step1
  patches and fusion, Regions, Step3, Step4, provenance suites; the CI list
  locally.
* Real machine (user): dataset switch and step changes with patches loading;
  close during a Regions detection.

## 6. Scope (closed whitelist)

* R2: `native/fusion_viewer_core/src/py/viewer.rs`, `Cargo.toml` /
  `Cargo.lock` (0.2.2), NEW `tests/test_core_level_check.py`
* A6: NEW `ui/step1_display_types.py`; `ui/step1_gpu_layer.py`,
  `ui/step1_gpu_binding.py` (move + re-import only); the import lines of
  `ui/step1_core_binding.py`, `ui/step1_core_labels.py`,
  `ui/step3_core_labels.py`, `ui/step3_label_binding.py`,
  `ui/step1_viewer_mount.py`; `ui/step1_core_adapter.py`
  (`requested_backend`); `ARCHITECTURE.md` (one line); NEW
  `tests/test_step1_display_types.py`
* C9: `ui/main_window.py` (`_stop`,
  `closeEvent`, `_retire_fusion_worker`),
  `ui/tma/workers.py` (`shutdown`), `ui/regions/page.py` (`shutdown`
  pass-through); NEW `tests/test_gui_waits.py`
* C10: `utils/error_hook.py` (`report`, `set_closing`, `Notifier._show`);
  `ui/main_window.py` (`_go_to_regions`, `_rm_save_view`,
  `_save_step1_session`, `closeEvent`, `mark_roi_step` caller ~10417,
  `_restorable_fused_zarr` (the catch at ~10822), `_rm_note_viewing`);
  `core/regions/document.py` (`set_session`); `ui/step0/step0_page.py` (`_rm_publish_correct_run`,
  `_rm_note_editing`, `rm_save_draft`, `rm_save_shared_windows`);
  `utils/run_store.py` (`write_inputs` optional argument);
  `ui/block01_display.py` (`apply_intensity_policy`); `core/provenance.py`
  (the five catch blocks); `workers/segment_merge_worker.py` (the two
  provenance blocks); `core/step0_handoff.py` (~402);
  `ui/step1_presegmentation/run_job.py` (~141); `core/regions/quant_apply.py`
  (`_pixel_size` + the provenance field); NEW `tests/test_protected_errors.py`
* this plan; `KNOWN_ISSUES.md` (the deferred C9 items)
* implementation exception (test fixture compatibility only, codex code
  review round 1 P3): `tests/test_ui_surface_contract.py` — its Regions
  `shutdown` double takes `timeout_ms`, and the retry timer is stubbed in
  that one refusal check

## 7. Questions for the user — answered

1. A6: option A (TG 2026-11-02).
2. C10.3: report and record, not refuse (TG 2026-11-02).

## 8. Codex plan review round 1 — answers

* P1 stale pixels after a zero wait → §3.2: eligibility ends at the stop
  (disconnect + removal from `_patch_loaders`), retired set until finished.
* P1 montage retired list insufficient → deferred.
* P1 Step0 pending Process not covered by busy / close / Stop → deferred.
* P1 session-helper rethrow can leave Fusion locked → helpers keep never
  raising; they report and keep their return values (§4.2).
* P2 pre-seg `close(0)` still blocks → deferred.
* P2 `error_hook.handle` best-effort → `report` with the installed log /
  notifier and a closing flag; tests use the real handler (§4, §5).
* P2 C10.3 misses `None` / unreadable manifest → covered (§4.3);
  `write_inputs` argument named in the whitelist.
* P2 acceptance / whitelist → rewritten (§5, §6: exact functions; docs named).
* P3 extraction inventory → complete list, moved unchanged (§2).

## 9. Codex plan review round 2 — answers

* P1 Regions refusal not on the entry path → `_go_to_regions` checks before
  switching; `set_session` skips an empty sha (§4.1); both whitelisted.
* P2 failure vs "nothing to write" → a failure is only a caught exception,
  counted through an optional per-call list; acceptance covers the no-op
  cases (§4.2, §5).
* P2 queued notices after `set_closing` → suppression at delivery in
  `Notifier._show`; the flag turned off on a refused close (§4, §5).
* P3 `_LabelTextureLru` dependencies → moved with `_as_name`,
  `_TextureRecord`, `_check_gl` (§2).
* P3 R2: version bump at the commit; the test's `set_view` box fixed to
  `[0, 0, 512, 512]` (y0, x0, y1, x1) and run against the rebuilt core;
  corrected-source wording fixed (§1); `_restorable_fused_zarr` named (§6);
  `cancel_join_thread` rationale corrected (§3.1).

## 10. Codex plan review round 3 — answers

* P1 a retired patch loader can rebind the shared `io_loader`'s corrected
  store → the zero wait for patch loaders is withdrawn (the 3 s wait stays);
  only the "survivor stays current" bug is fixed (§3.2). The shared-reader
  question goes to `KNOWN_ISSUES.md` with the other deferred waits.
* P3 helpers not fully guarded today → the unguarded parts come inside the
  `try` (§4.2).
* P3 manifest failure ≠ identity unavailable → distinguished; a computed id
  is kept (§4.3).

## 11. Codex plan review round 4 — answers

1. P2 retired loader escapes the "still stopping" refusal → §3 item 2:
   `_retired_loaders` is per patch and checked by `_stop_loader_for` and
   `_stop_all_loaders` like a current loader. Acceptance (§5, synthetic,
   a fake loader thread that ignores stop): stop times out → loader retired
   (its signals no longer reach the window) → a repeated request for that
   patch while it is still alive is refused ("still stopping") and starts
   no second loader → the thread finishes → the retired entry is gone → the
   next request starts a loader normally.
2. P2 "Close anyway?" repeated by automatic close retries → §4 item 2: asked
   only at the final acceptance point, after all retry gates. Acceptance:
   a save helper that always fails + a live fake worker that ends after two
   retries + the answer Yes → exactly one question, the window closes by
   itself; with No → the window stays and works.

## 12. Codex plan review round 5 — answer

P2 a notice already open survives an accepted close → §4 "Reporting":
`set_closing(True)` closes the open notice box and releases a pending
reservation it drops. Acceptance (real `Notifier`, real event loop, the
confirmation answered by a patched `QMessageBox.question`): a save helper
that always fails + a live fake worker ending after two retries → Yes:
exactly one question, no notice window left, the window closes and
`QApplication` can quit; No: the window stays usable and a later `report`
shows its notice.

## 13. Patch loaders — taken out, own plan next (user 2026-11-02)

User principle: switching dataset / step / saving a correction and patch
reading are decoupled — a switch never waits for patch reads. User's
proposal (accepted in principle, to be planned and reviewed separately,
right after this block and before the Step2 TMA mode): every data version
(dataset switch, a new saved correction) gets its OWN image reader object,
never rebound in place; readers started for the old version are marked
void, their results discarded, and they finish on their own (a thread in a
blocking read cannot be killed safely) and release the old reader; the
patches on screen are re-requested with the new reader at once. Needs a
survey of every user of the shared `io_loader` (patch loaders, viewers,
`saved_correction.register_loader`, workers) and user authorization as a
new mechanism. §11 item 1 and the v2.2-v2.4 retired-loader text are void.

## 14. Codex code review round 1 — answers

* P2 Save Session button passed `clicked(bool)` into `failures` → the
  button calls `_save_step1_session()` with no argument.
* P2 automatic-close acceptance → `tests/test_gui_waits.py` now runs the
  window's REAL retry timer: one user close, the window closes by itself
  (Regions job; failed save + live job + Yes: one question, no top-level
  window left); No keeps it and notices work.
* P2 protected-error coverage → added: a site through the real `report`
  (log once, notice once), pending restore / unchanged content are not
  failures, fusion end unlocks the UI with a failing session save and ROI
  index, `_rm_note_viewing`, the pixel-size reason through the real
  `provenance_summary`.
* P3 fixture change outside §6 → recorded in §6.

## 15. Codex code review round 2 — answer

* P2 per-site coverage → `tests/test_protected_errors.py` now forces a
  failure at EVERY changed site through the real `report` (log once, one
  notice, prefixed by the site): shared windows, Step0 draft, Step1 session,
  interrupted-publish recovery, Step0 ROI index (a real `write_handoff`),
  pre-segmentation `.done` (a real `PresegRunJob._main`), both remap
  provenance writes, the four provenance siblings and `safe_register`,
  `_rm_note_viewing`, ROI step status at fusion end, Intensity lock, slide
  identity / manifest at Step0 Save.

## 16. Codex code review round 3 — answers

* P2 No after the close retired a RUNNING fusion left Step1 locked (its
  completion can no longer unlock) → on No, when no fusion job remains and
  Step1 is still locked (`config` is disabled only by `_lock_ui`), the window
  unlocks and says the fusion was stopped. Test: a real running job, Step1
  locked, close, the job ends, No → the window stays and is unlocked.
* P3 real-report coverage → the `reports` recorder now passes every report
  to the REAL `report` and checks at teardown that each is in the log
  exactly once and a notice was asked for; the per-site check asserts the
  notice appeared. §15's claim now holds.
