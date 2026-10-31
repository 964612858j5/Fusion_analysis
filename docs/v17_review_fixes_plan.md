# v17 review fixes — plan v2.1 (2026-10-31)

v2: codex plan review round 1 (2 P1 + 4 P2) answered — see §7.
v2.1: round 2 — no P1/P2; its P3 (session test wording) applied in §1.

Source: the whole-code reviews of 2026-10-31 (codex astra high, Fable 5.1
medium). User ruling (TG 2026-10-31):

* Step2's HQ / HQ2 / CSD are no longer maintained: their failed-tile bug is
  NOT fixed; instead these methods must not appear in Block01.
* Fix: Step0 Save→Cancel overwriting the previous settings; non-atomic
  project files; CSV precision; the small errors (unreachable warning,
  unknown method silently replaced, code version without a dirty mark).
* Hide Step4's `Batch...` button for now.
* Everything else (global exception hook, resource lifetimes, CI, packaging,
  large changes) comes AFTER this block.

Scope is a closed whitelist (AGENTS.md rule 1): only the items below. No new
cache, scheduler or state machine. Code of the retired methods stays in the
repo (deleting it would be a separate block).

Base: v17 at 664b9a3 plus the uncommitted on-top-window change
(`ui/widgets/away_hider.py`), which is committed on its own once accepted.

---

## 1. HQ / HQ2 / CSD do not appear

Today Step2's `Method:` box and Step1's embedded legacy panel
(`ui/step0/search_ctrl.py`, mounted at `ui/main_window.py:1330`) list every
key of `SEGMENTATION_METHODS` through `available_segmentation_methods()`
(`utils/segmentation_config.py:344`), so the three methods can be chosen and
run. Step1's method editor already hides them (`UI_METHODS`).

Changes:

1. `utils/segmentation_config.py`: `RETIRED_METHODS = (CELLPOSE_NUCLEI_HQ,
   CELLPOSE_NUCLEI_HQ2, CELLPOSE_NUCLEI_CSD)`; `available_segmentation_methods()`
   leaves them out (its only callers are the two method boxes above);
   `is_retired_method(m)`. `segmentation_param_schema.HIDDEN_METHODS` becomes
   that tuple.
2. Step2's parameter-index `Method:` list (`ui/step2_page.py:1596`) skips
   retired methods; an index whose active method is retired selects the first
   remaining one, and an index with only retired methods says so.
3. Applying a configuration that names a retired method — a parameter file,
   an index entry, a restart draft (`apply_draft`), Step1's handoff, the legacy
   panel's file load — is refused with one message ("<name> is no longer
   available in Block01"), nothing applied. Today `_apply_seg_config_to_ui`
   (`ui/step2_page.py:2545`) silently keeps the previous method when
   `findData` fails; that silent keep becomes the refusal (also for any other
   method the box does not list).
   Step1's "Load Previous Step1 Session" (`ui/main_window.py:5086`) hands the
   session's `p2_params` to the legacy panel AFTER restoring the rest; there
   the check runs FIRST, before any state is restored: a retired or unknown
   method in `p2_params` -> those parameters are dropped with one notice and
   the rest of the session restores as today (a session is mostly display and
   handoff state that is still valid).
4. Step2's Run refuses a retired method (defence in depth: a configuration
   that reached the worker by another route).

Not changed: Step3 / Step4 reading of runs. User (TG 2026-10-31): no run
made by these methods exists, so nothing is needed there. Their UI sections in Step2
(HQ2 / CSD parameter groups) become unreachable and stay in code.

Tests: the method boxes list no retired method; applying each
configuration-only source (parameter file, index entry, draft, Step1
handoff, legacy panel load) with a retired method is refused and changes
nothing; a Step1 session whose `p2_params` names a retired or unknown method
restores its other fields, does not apply those parameters, and shows one
notice; Run refuses; existing
HQ tests that drive the worker directly stay (the worker is not changed).
Tests that select a retired method through Step2's UI are updated or moved to
the worker level.

## 2. Step4 `Batch...` hidden

`ui/step4_page.py:483`: the button is created as now and hidden
(`setVisible(False)`), with a comment naming the review finding (it globs the
legacy `segmentation_runs/seg_*` layout, quantifies only the first region and
reports success when every sample failed). The dialog code stays. Test: the
button is not visible on the Step4 page.

## 3. Step0 Save → Cancel keeps the previous settings

Today `_write_current_correction_config()` (`ui/step0/step0_page.py:12277`)
overwrites the workspace's `step0/correction_config.json` and sets the
loader's correction config BEFORE the run is prepared and computed; Cancel
removes the new run (`_rm_discard_pending`) but not the new settings, so the
previous published product is paired with settings it was not made with
(Step1 reads them, `ui/main_window.py:4005`; the loader applies them to raw
pixels for channels missing from the store, `core/io_loader.py:119`).

Change (smallest that covers every exit):

1. Before writing, `_write_current_correction_config` keeps the previous
   state, independent of any pending run: the absolute config path and its
   bytes (or "no file"), the loader object, its `correction_config`, and its
   corrected-store binding (path + decisions; a small read-only accessor on
   the loader, `core/io_loader.py`) together with the page's
   `_applied_corrected_decisions`. The new file is written with the strict,
   cleanup-capable `core.provenance.write_json_atomic`.
2. Restoring (`_rm_restore_settings`): the kept file back atomically (or
   removed if there was none), the loader's config back, and — if the store
   binding was changed by this Save — the previous binding back through the
   existing `_apply_corrected_store(previous_path, previous_decisions)`, which
   also reconciles the preview cache (reviewed P1: all three completion
   branches apply the new store BEFORE `_emit_complete`, so a later remap or
   handoff failure otherwise leaves the loader on the failed result). Then
   the kept state is forgotten. Skipped if the loader is no longer the same
   object (dataset switch: the old loader is gone; only the file is put back).
3. Restoring is called from `_rm_discard_pending(reason)`, which every
   handled unfinished exit already uses (Cancel while preparing or
   computing, a correction error, a failed copy, a failed viewer hand-off, a
   dataset switch, a failed remap or handoff write).
4. Exits that today raise without it (reviewed P1) get it explicitly: the
   no-correction branch's run and empty-zarr creation (`step0_page.py:12313`)
   and `_rm_prepare_save`'s run creation (`:11924`) — a try/except that
   restores and re-raises.
5. "superseded by a new Save" inside `_rm_new_correct_run` (it runs after the
   new settings were written) neither restores nor clears the kept state
   (`_rm_discard_pending(..., settings=False)`).
6. A Save that completes (`_emit_complete` returns True) forgets the kept
   state.

Existing limits, documented and NOT changed here (no multi-file transaction
in this block): a full Save's handoff (`core/step0_handoff.py:387`) replaces
the fixed correction / ROI / patch files one after another before the
manifest, and publishes the run early (`:269`, which clears
`_rm_pending_run`). A failure AFTER that point can leave a published,
unreferenced run and some replaced ROI / patch files; this block puts back
the correction settings and the loader binding, and never deletes a
published run.

Tests (synthetic workspace), split by when the failure happens:
before publication — Cancel while preparing, Cancel while computing, a
correction error, run creation failing, empty-zarr creation failing, a first
Save (no previous file) cancelled -> no file; after the store was applied —
a remap / handoff failure: previous config bytes, previous loader config, and
reads of a corrected channel return the PREVIOUS product's pixels. A
completed Save keeps the new settings and store.

## 4. Project files written atomically

Only files that are rewritten in place while holding good content (a failed
write can lose what was there). Writers move to the existing strict,
cleanup-capable `core.provenance.write_json_atomic` (`:77`; already used by
`utils/roi_project.py:152` and `utils/trash.py`). Not `utils/run_store`'s
copy: it leaves its temporary file on a failed dump and serialises unknown
objects with `default=str`, which would change what these files accept.

| file | writer |
|---|---|
| project `roi_index.json`, workspace `roi_manifest.json` / `roi_index.json` | `utils/roi_project.save_json` (`:114`, 6 callers) |
| parameter index | `utils/segmentation_params.py:66` |
| channel remap config | `utils/channel_remap_config.py:317` |
| Step1 session autosave | `ui/main_window.py:4759` |
| Step2 runtime remap (`channel_remap_config.step2ready.json`) | `ui/step2_page.py:3023` |
| Step0 `correction_config.json` | item 3 |

Not changed: files inside a run folder before its `.done` (the run is not
published until then: `run_metadata.json`, Step4 `.partial` files), files
already staged and renamed (plan store, fusion settings, step0 handoff), a
user-chosen export path, and the unreachable Step1.5 page.

Tests: a serialisation failure and a replace failure (patched `os.replace`)
each leave the previous file whole and no temporary file behind, for
`save_json`, the parameter index and the remap config; an unserialisable
value still raises as before.

## 5. CSV keeps full precision

`workers/feature_extract_worker.py:128` writes every non-integer column with
`%.6g`. The CSV path is float64 by contract: CSV extraction forces float64
storage (`:296`), `block()` (`core/quant_engine.py:603`) returns float64, and
the h5ad is a separate float32 downcast. New format: `%.17g` for every
non-integer column (round-trips float64), integers `%d` as now; Regions
columns already use `repr` (unchanged).

Tests: CSV values read back equal the float64 results exactly (including
centroids above 1e6 px); their float32 conversion equals the h5ad values.
The existing test expecting `%.6g` (`tests/test_step4_worker.py:292`) is
updated.

## 6. Small errors

a. **Unreachable warning** (`ui/main_window.py:4369`): the same condition is
   tested twice; the first returns with a print, so the "Raw OME-TIFF path
   missing" warning never shows. One test: print, and the warning when not
   `auto`. Test: a manifest without a raw path warns when not auto, stays
   quiet when auto.

b. **Unknown method silently becomes the default**
   (`utils/segmentation_config.py:363`): a non-empty method name not in the
   registry raises `ValueError("Unknown segmentation method: …")`; an empty
   name still means the default. Callers that read a file already catch and
   report (Step2 parameter file `:1668`, legacy panel loads
   `search_ctrl.py:1160/1186`); `apply_draft` reports and applies nothing;
   `_same_effective_settings` (`:1368`) treats an unknown method as "not the
   same". Internal callers pass registry names only.
   Tests: an unknown name is refused by each file-reading path with a
   message; the empty name still gives the default.

c. **Code version without a dirty mark** (`core/provenance.py:236`, copies in
   `workers/feature_extract_worker.py:75` and `seg_runner/runner.py:41`):
   `git_commit()` returns `"<hash>-dirty"` when code differs from the commit —
   tracked or untracked `*.py`, `*.rs`, shader files (`git status --porcelain
   -- '*.py' '*.rs' '*.frag' '*.vert'`; other files such as `cufile.log` or
   review notes do not count). Computed once per process, so it describes the
   code that was loaded at start. `feature_extract_worker` uses the core
   function; `seg_runner` (runs in the engine environments) keeps its own copy
   with the same rule.
   Tests: on a synthetic git repository, clean → hash; a modified `.py` →
   `-dirty`; a modified non-code file → hash.

---

## Verification

* New tests per item above; tests touched by the method-list change updated.
* Targeted regression: Step0 save/cancel/handoff, Step1 handoff, Step2 page /
  restart / remap / engine, Step4 page / worker / CSV, provenance, roi
  project, segmentation config; 4 in parallel, GPU display `:1`.
* Codex astra high review of this plan, then of the code.
* Real-machine acceptance (user): Step2 method list without HQ/HQ2/CSD;
  Step4 without Batch; Step0 Save → Cancel then reopen keeps the old
  settings; a CSV exported and compared with the h5ad.

## 7. Codex plan review round 1 — answers

| finding | answer |
|---|---|
| P1 some Save failures bypass restoring | §3 points 1, 4, 5: kept state independent of a pending run; run / empty-zarr creation failures restore explicitly; "superseded" neither restores nor clears |
| P1 loader stays on the failed store | §3 point 2: the previous store binding restored through `_apply_corrected_store`; tests read pixels |
| P2 CSV dtype | §5: `%.17g`, float64 contract; tests against float64 and float32/h5ad; old test updated |
| P2 Step1 session path | §1 point 3: checked before restoring, parameters dropped with one notice |
| P2 atomic writer choice | §4: `core.provenance.write_json_atomic`; replace-failure tests |
| P2 handoff limits | §3 "Existing limits" rewritten; tests split before / after publication |
| dirty capture time | §6c: captured at start-up |
