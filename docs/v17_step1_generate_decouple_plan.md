# Step1: Generate the segmentation input without a pre-segmentation run — plan v1.2, 2026-11-10 (codex rounds 1-2, §7-8; user rulings §5, v1.2)

Requested by the user (TG 2026-11-10): generating Step2's input should not
need a pre-segmentation run — the user can set the segmentation parameters
in Step2 by hand. Step1 keeps one button, "Generate DAPI input zarr"; when a
pre-segmentation result was chosen (Use), its parameters are saved as well,
and Step2 uses them by default. Plan only; implemented after the Ubuntu
StarDist push, with the user's approval.

## 0. User rulings (TG 2026-11-10)

1. The chosen result's parameters are saved **when Generate is clicked**
   (not at Use).
2. Parameters and image from different fusion settings: **Step2 warns**
   before the run (Run anyway / cancel), as for an engine version change.
3. **One image for every method**, its nucleus layer at **weight 1** (full
   DAPI brightness, as the nucleus-only methods use today).
4. Step1 no longer saves hand-set parameters: Step2's manual source covers
   that.

## 1. Today (read-only check, HEAD 53a8f51)

* One button (`ui/main_window.py:1485-1489`, text at `:10131-10134`):
  "Save segmentation config && Generate DAPI input zarr" ("Save Config &&
  Generate fused.zarr" for Cellpose whole-cell). `_save` (`:11458`) does both:
  writes the active segmentation params (`save_segmentation_params`, `:11651`)
  and starts the fusion (`FullFusionWorker`).
* It is enabled only after a pre-segmentation result was chosen with Use
  (`_save_allowed`, `:10687-10694`, user ruling 2026-09-25), and refused when
  the params came from other fusion settings
  (`_params_match_committed_settings`, `:10188`, `:11507`).
* The image depends on the method: **Cellpose whole-cell** →
  `step1_fused_zarr` with the nucleus at the fusion settings' weight
  (`_expected_fused_zarr_meta`, `:11042`); **every other method** (the
  nucleus methods AND Mesmer whole-cell / nuclear-guided) →
  `step1_dapi_input_zarr`, nucleus weight forced to 1 (`:11661-11672`,
  `_expected_dapi_input_meta`, `:10904`). Both: ch0 marker fusion, ch1
  nucleus (`nucleus = clip(weight · DAPI)`, `core/fusion_engine.py`);
  Mesmer reads `[ch1, ch0]` (`core/preseg_input.py:109`).
  Each identity carries `purpose`, `method`, `artifact_kind`; in a workspace
  the reuse key is that identity without `config_hash`, paths, times and
  some nested fields (`ui/main_window.py:11751`), compared field by field by
  `utils/run_store.py:267` — so the other method's image is not reused.
* Step2 loads the active params when its params field is empty
  (`ui/main_window.py:5718`; `:5661` not when the workspace already has
  Step1 output); a session restores the old params file
  (`ui/step2_page.py:1399`) and then the Step2 draft (`ui/main_window.py:5736`).
  So a SECOND Generate does not reach Step2 by itself today.
* Readers of the two kinds (traced by codex): only Step1's identity builders
  and checks (`ui/main_window.py:10904, 11042, 11008, 11296`), the writer
  (`ui/step0/overview_panel.py:317, 835`, no per-kind pixel logic), run_store
  reuse (generic field compare), provenance (`core/provenance.py:441`, a
  recorded field). Step2/3/4 do not branch on the kind; Step2's input order
  comes from the method (`core/preseg_input.py:109`). One kind breaks no
  reader.
* Step2 auto-loads the active params (`ui/step2_page.py:1751`, `:2842`), has
  a "None / Manual default" source, and already asks before a run when the
  Step1 engine identity differs (`_confirm_engine_identity`, `:3594`). The
  pre-segmentation contract carries `fusion_settings_hash`
  (`core/preseg_contract.py:26, 85`).

## 2. Proposed behaviour

* Step1 button: **"Generate DAPI input zarr"** (one text for every method).
  Enabled when the fusion settings are saved and the existing Generate
  checks pass (committed settings, every weighted channel has a window, the
  Step0 correct run, ROI-only output with an ROI); no pre-segmentation needed.
* What it writes: **one image kind** for all methods — ch0 the marker fusion
  of the committed settings, ch1 the nucleus at weight 1: the existing
  `_expected_dapi_input_meta` path for every method, its identity (and so
  the reuse key) WITHOUT `method`; `artifact_kind` / `purpose` stay
  `step1_dapi_input_zarr`. Existing images are not reused once (a new
  identity); existing segmentation results keep their own method (a result's
  method is result meaning, untouched). The fuse run already stores its full
  `fusion_settings` incl. the hash (`ui/main_window.py:11813`): Step2 reads it
  from the input's run — **no new zarr attribute, no worker change**. The
  run's recorded `method` (`:11811`) is dropped from the identity but may
  stay as a note.
* If a pre-segmentation result is chosen (Use) when Generate is clicked, its
  parameters are saved as the active Step2 parameters, exactly as today
  (`_preseg_segmentation_config` → `save_segmentation_params`); Use itself
  stays a selection only (`ui/main_window.py:7530`). The existing selection
  checks stay (`:7441` drops a stale selection, `:7477` checks validity);
  only the refusal "params from other fusion settings" (`:11507`) goes —
  that mismatch is shown in Step2. The hand-set-params save branch after
  `:11577` goes; a restored old `_p2_params` without a valid Use is not
  saved.
* **Step2 picks up the new params** (codex P2): after a Generate that saved
  params, Step2's params field is set to the new active params file the
  next time Step2 is shown (instead of only when the field is empty), and
  the loaded params replace the method/params on screen — also over a Step2
  draft the user edited by hand (user ruling §5 Q3: the new Generate's
  params win). A Generate WITHOUT a chosen result sets Step2 to "None /
  Manual default" the same way (§5 Q1). A Step2 draft restored on restart is
  kept unless a Generate happened since (the Generate is the newer choice).
* Without a chosen result, Generate writes the image only. The old active
  params file stays on disk (not deleted), but **Step2 does not take it**:
  after such a Generate, Step2's source is "None / Manual default" — the user
  sets the parameters (user ruling §5 Q1). If the user then chooses the old
  params on purpose (the index source or Browse) and their
  `fusion_settings_hash` differs from the input's (the condition below), the
  question tells them so, and Run anyway continues.
* Step2: input = the generated image; params = the params the LAST Generate
  saved (a Generate with a chosen result), else "None / Manual default" (a
  Generate without one) — not simply "any active params file". Before a run, when the
  params' `fusion_settings_hash` (the contract's) differs from the input
  run's `fusion_settings.hash`, Step2 asks ("these parameters were found on
  other fusion settings: Run anyway / Cancel"). Without either hash (an old
  image, an input outside a workspace, manual params) nothing is asked.
  Run anyway is recorded in the run's own seg config
  (`seg_config["settings_mismatch_confirmed"] = {params_hash, input_hash}`,
  written with the run's params as today) — the worker does not check it,
  no new enforcement.

## 3. Closed whitelist

* `ui/main_window.py`: button text and enablement (`:1485-1489`,
  `:10131-10141`, `_save_allowed` / `_check_save_unlock`, `_lock_ui` /
  `_unlock_ui`), `_save` (no Use required; params saved only with a valid
  Use; the hand-set branch and the `:11507` refusal removed), the identity
  (one builder, no `method`), the Step2 hand-over of new params (`:5661`,
  `:5718`).
* `ui/step2_page.py`: reading the input run's `fusion_settings.hash`, the
  question before a run, the record in `seg_config`, taking a new active
  params file.
* Tests — existing, adjusted only where they assert the old rule:
  `tests/test_step1_save_params_file.py`, `tests/test_step1_preseg_run_ui.py`,
  `tests/test_step1_step2_handoff_e2e.py`,
  `tests/test_step1_fusion_settings_commit.py`, `tests/test_preseg_contract.py`
  (read only unless it asserts the refusal), `tests/test_v16_a6_workspace.py`
  (reuse); new: `tests/test_step1_generate_decouple.py`.
* `UI_SURFACE_RULES.md` (Step1 button, Step2 question),
  `docs/user_guide.md`, `docs/用户指南.md` (Step1 / Step2 paragraphs).
* Not changed: `ui/step0/overview_panel.py` (the writer),
  `workers/segment_merge_worker.py`, `utils/run_store.py`, `core/*`, the
  Viewer. No new registry, cache or state machine.

## 4. Tests and acceptance

* Generate enabled with saved settings and no pre-segmentation; disabled
  without saved settings; with a chosen result it also saves the params.
* The image is the same for every method (same identity, ch1 = DAPI at
  weight 1); a second Generate on the same settings reuses it.
* Step2: no Step1 params → manual default; params from other settings →
  the question; Run anyway recorded; Cancel runs nothing.
* A second Generate after a new Use reaches Step2 (codex P2 path:
  Generate A → Step2 → back, Use B → Generate → Step2 shows B); a reused
  image; a restart (old session, Step2 draft) as ruled in §5 Q3.
* Mutations only as a test technique for the approved rules. Real machine:
  Step1 without pre-segmentation → Generate → Step2 manual run; with
  pre-segmentation → Generate → Step2 default params; change the fusion
  settings, Generate again without Use → Step2 asks.

## 5. User rulings (TG 2026-11-10)

1. Generate without a chosen result: Step2's parameters are set by the user
   ("None / Manual default"), not the old ones by default; old params chosen
   on purpose → the "other fusion settings" question, Run anyway allowed.
2. The kind keeps the name `step1_dapi_input_zarr`.
3. After a Generate with a chosen result, Step2 uses the new params, also
   over a hand-edited Step2 draft.

The questions as asked:

1. Generate WITHOUT a chosen result while an older active params file
   exists (from an earlier Generate): keep it (Step2 then warns if the
   settings differ) — recommended — or clear it so Step2 starts from the
   manual default?
2. The image's name/kind: keep `step1_dapi_input_zarr` (recommended), or a
   neutral new name?
3. A new Generate with params versus a Step2 draft the user changed by hand
   since: the new Generate's params win (recommended — the user has just
   chosen them in Step1), or the hand-edited Step2 draft is kept?

## 6. Investigated (done in v1.1, see §1; kept for the record)

* Every reader of `artifact_kind` / `purpose` / `step1_fused_zarr` (Step2,
  Step3, Step4, provenance `core/provenance.py:441`, run_store reuse) — a
  single kind must not break a reader that expects the whole-cell one.
* Mesmer's fused mode reads ch0 as membrane: unchanged (ch0 is the same).
* Cellpose whole-cell with ch1 at weight 1 instead of the settings' weight:
  per-image normalisation in the engines makes a uniform scale nearly
  irrelevant (user ruling 3 accepts it).
* Old sessions whose `last_save` / pending params assume the old button.

## 7. Codex plan review round 1 (astra low) — FAIL (P2 ×1, P3), taken in v1.1

* P2: a second Generate did not reach Step2 (auto-load only into an empty
  field; session restore order) → §2 "Step2 picks up the new params", §4
  test of that path, §5 Q3.
* P3: the zarr attribute would have needed the writer → replaced by the
  fuse run's stored `fusion_settings.hash` (codex advisory); whitelist made
  concrete file by file; the confirmation recorded in `seg_config` only.
* P3: facts corrected — the branch is Cellpose whole-cell vs every other
  method (Mesmer included); the reuse key is the identity minus
  `config_hash`; Step2's auto-load conditions. §6's tracing done (§1).
* Kept the existing selection checks (`:7441`, `:7477`); Use stays a
  selection; the hand-set save branch is removed explicitly.

## 8. Codex plan review round 2 (astra low) — PASS

No new public-path defect; the new approach (the input run's stored
`fusion_settings.hash`, the confirmation in `seg_config` only, Step2 taking
the new active params through its existing loader) is workable; whitelist
closed. Open: the user's rulings §5 Q1-Q3 (the draft-vs-new-params order
and the session restore order follow Q3). Hardening stops.

## 9. v1.2 — user rulings applied

§2 and §5 updated with the rulings: Generate without Use → Step2 manual
default (old file kept, not taken by default; chosen on purpose → the
question); Generate with Use → Step2 takes the new params over a draft. Same
whitelist (the Step2 hand-over in `ui/main_window.py` / `ui/step2_page.py`);
§4 tests add: Generate without Use → Step2 manual default; choosing the old
index then asks.

Codex quick check of v1.2 (astra low): PASS; wording advisories taken (Step2's
default follows the last Generate; the question's condition is the hash
mismatch). Plan complete; waits for the Ubuntu push, then implementation.

## 10. Implementation (2026-11-10, on 8b3c2ae; user: "可以，开始吧")

The line references of §1 still hold at 8b3c2ae (the StarDist commit did not
touch `ui/`).

* `ui/main_window.py`:
  * button `Generate DAPI input zarr` for every method (no per-method text);
  * `_save_allowed`: a loaded slide, no fusion job running, committed and
    not dirty fusion settings; `_check_save_unlock` follows it and is called
    from `_update_fusion_settings_state` (every commit / dirty change), the
    method change, a dropped selection and the dataset reset;
  * `_save`: no Use needed; the hand-set / `direct_method` branch, the "No
    parameters" dialog and the "params from other settings" refusal are
    gone; params are saved only for a valid Use (`_params_source ==
    PRESEG_SOURCE`, then `_preseg_selection_valid`, as before); one image
    kind (`_expected_dapi_input_meta`, nucleus weight 1) whatever the method;
    `_expected_meta_for_current_config` the same; `method` left out of the
    identity (the `_method` argument is ignored, kept for older callers);
    the fuse run's params.json keeps `method` as a note (may be empty);
  * the Step2 hand-over: `_save` sets `_pending_step2_handover`
    (`("params", OUTPUT_DIR)` with a Use, else `("manual", "")`);
    `_on_fusion_done` (also the reuse path) makes it current, turns off the
    once-per-session draft restore and applies it at once
    (`_apply_step2_handover`); while a Step2 run is going it waits and is
    applied on the next `_go_to_step2`.
* `ui/step2_page.py`: `use_manual_default()` (source manual, the method's
  defaults, the loaded index kept); `_input_fusion_settings_hash()` (the
  input's fuse run `params.json` → `fusion_settings.hash`);
  `_confirm_fusion_settings_match()` asked in `_run` for the index source
  after the engine-identity question; Run anyway →
  `seg_config["settings_mismatch_confirmed"] = {params_hash, input_hash,
  confirmed_at}` (written with the run's params; the worker does not check
  it).
* Tests: new `tests/test_step1_generate_decouple.py` (8); adjusted where
  they asserted the old rule: `test_step1_fusion_settings_commit.py`
  (searched-on-other-settings params: not refused, not saved),
  `test_step1_save_params_file.py` (old panel params: not saved),
  `test_step1_step2_handoff_e2e.py` (old Phase 2 params: neither needed nor
  saved), and — outside the §3 list, one line —
  `test_step1_result_publication.py` (the "other kind" case is now
  `step1_fused_zarr`).
* Docs: `UI_SURFACE_RULES.md`, `docs/user_guide.md`, `docs/用户指南.md`.
* Mac: the affected modules (14) give the same failures as HEAD (12, all
  pre-existing: Rust fusion parity and four preseg-UI cases) and no new
  one; the 8 new tests pass; mutation check: without the hand-over or
  without the question, 3 of them fail.
* Restart (§5 Q3): the hand-over is applied to the Step2 page at once, and
  with the draft restore off, closing writes the page's draft
  (`_rm_save_page_drafts`) -- so the next session restores the Generate's
  choice, not the older draft; nothing new is persisted. Known limit: a
  hand-over still waiting for a Step2 run when the app is closed is lost
  (the page's draft is saved as it was).

## 11. Codex implementation review round 1 (astra low) -- FAIL (P2 x1), fixed

* P2: Generate without Use before Step2 was ever shown in the session (its
  index field empty): the first entry auto-loaded the older active params
  over the manual choice. Fix: the manual hand-over carries OUTPUT_DIR and
  `use_manual_default(index_dir)` loads the index without applying it, so
  the entry's "load when empty" no longer fires. New test
  `test_a_generate_without_a_choice_before_step2_is_ever_shown` (fails
  without the fix). Same affected-module failures as HEAD, none new.

Round 2 (astra low): **PASS** -- the round-1 P2 is resolved (first entry, reuse, the workspace path); no new public-path defect; hardening stops (AGENTS rule 7).
