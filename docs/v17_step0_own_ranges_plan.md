# Step0 keeps its own Min/Max/Gamma — plan (v4, 2026-10-27)

v1-v3 were REJECTED by review (codex_s0ranges_plan_1..3.out). v4 answers
every finding (§8 v1, §9 v2, §10 v3) and records the user rulings taken
after the first review (§1).

## 1. Report and rulings

User acceptance report (2026-10-27, project `/sdb1/Fusion/TMA1_test`, slide
`Fusion_Final_20260824_Slice1_Scan1.qptiff`): "从step1切换回step0后，step0之前的通道选择和
minmaxgamma会被改变，应该维持之前的设置".

Replayed on a private copy (copy bind-mounted over the project path in a private
mount namespace, slide read-only; runner `a9_bench/s0a/user/run_user.sh`):

| channel | set in Step0 | after Step1 -> Step0 | Step1 saved `display_mapping` |
|---|---|---|---|
| DAPI   | 0 / 40 / 0.7  | 1 / 105.2 / 1 | 1 / 105.197 / 1 |
| CD45RO | 0 / 20 / 0.8  | 1 / 94.9 / 1  | 1 / 94.896 / 1 |
| HsBAg  | 0 / 30 / 1.3  | 1 / 255 / 1   | 1 / 255 / 1 |

Selection and ticks survived (already step-scoped). The windows did not: Step1's
committed-settings restore calls `Step0Page.adopt_intensity` (ruling Q2a,
2026-10-02) and there is ONE window table for all steps.

USER RULINGS 2026-10-27:
* R1. **Step0 keeps its own windows; Step1/Step2/Step3 (and Regions) keep
  sharing one set as today.** Adjusting a window in Step0 does not change
  Step1's windows or fusion; nothing Step1 does changes Step0's.
* R2. **An Intensity-only Step0 Save (same correct run) does not touch Step1's
  windows.** Only a handoff that publishes a NEW correct run (or the first
  handoff) starts Step1 from Step0's windows.
* R3. **Step1's unsaved windows are remembered across restarts** in Step1's own
  draft; they become official only with "Save fusion settings".

## 2. Today's architecture (one table, three mirrors)

(Unchanged from v1, corrected per review.)

* `ChannelDisplayState` (`ui/block01_display.py`): per dataset namespace
  `ns.mappings[(channel, nucleus)] = (lo, hi, gamma)`; reads `mapping`,
  `mapping_or_seed`, `mapping_in`, `mappings`; writes `set_mapping`,
  `adopt_mappings`, `_install_into`; seeds through `mapping_or_seed` (sync,
  seed port) and `Block01DisplayServices.request_mapping_seed` /
  `_on_mapping_seeded` (async, `workers/display_seed_worker.py`).
* Persistence port `Step0Page.write_display_window` -> workbench `wb._params`;
  reverse `_adopt_workbench_window` (on `wb.params_changed`; DAPI also writes
  `nucleus=True`). `_display_fallback` holds windows for channels the workbench
  does not know.
* The workbench (`ChannelWorkbench`) is the ONE Intensity window for every step.
  It is a generic widget (Step1.5/Step3 use it too). `set_channel_images`
  rebuilds `_params/_seeded`, `clear_channel_images` drops all three stores,
  `_install_loaded` seeds the provisional 0..1 entries on pixel arrival.
* Workbench-table readers: Step1 draft (`MainWindow._display_mapping` ->
  `display_mapping_for_preview` -> `display_mapping_draft`), the commit draft
  (`_fusion_settings_draft`, main_window.py:9743), fusion identity
  (`_fusion_display_mapping`, 10317: run params -> committed snapshot -> live
  draft), the run's `intensity` record; Step0 Save (`wb.build_config` ->
  `step0_channel_remap.json`, hashed into the handoff by
  `step0_handoff.py:299`), Save dirty check (step0_page.py:4569), `rm_save_draft`
  (`drafts.step0.intensity`), reopen (`_workspace_intensity` = remap json
  overlaid with the Step0 draft, step0_page.py:11185), Q2a `adopt_intensity`
  (merges into the same pending dict, 11261), preview provider metadata
  (`preview_source_provider.py:113,170`).
* State readers: Step0 page; main_window.py:445 (Step2/Step3 tissue context) and
  3196 (Step1/Regions); step1_viewer_binding 210/225; step1_draft_spec 173;
  step2_viewer 90; step1_viewer_mount 996 (GPU seed requests);
  step1_compose_binding 197 (CPU mapping notification); Step0 engine display rows
  (engine_mount.py:376).

## 3. Design

### 3.1 Ownership: two named tables, explicit everywhere

Two tables, named by OWNER, not by scope string:

* `SHARED` — Step1, Step2, Step3, Regions, and the "" scope.
* `STEP0` — Step0 only.

`table_for_scope(scope)` maps a display scope to its table; everything that
compares "same table?" uses it (Step1/2/3/Regions share).

**Readers and writers name their table; the default is SHARED.** Every existing
non-Step0 call site therefore keeps its meaning without edits, and a Step1
background read while Step0 is on screen still reads SHARED (no dependence on
the current scope). Step0's call sites pass `table=STEP0`. The current scope is
used for exactly one thing: which table the ONE Intensity window shows (§3.3).

### 3.2 Display state

* `DisplayNamespace.mappings` becomes `ns.tables = {SHARED: {...}, STEP0: {...}}`
  of `(channel, nucleus) -> (lo, hi, gamma)`; `ns.mappings` stays as the SHARED
  table (alias) for code that touches it directly.
* `mapping / mapping_or_seed / mapping_in / mappings / set_mapping /
  adopt_mappings / forget_mappings` take `table=SHARED`.
* `_install_into(payload)`: `payload["mappings"]` goes to SHARED;
  `payload["step0_mappings"]` to STEP0. `prepare_restore` forwards `scope` in
  the moved-identity branch too (finding 8). `_restore_snapshot` deep-copies the
  namespace (both tables).
* Signals: `mapping_changed(channel)` keeps meaning "SHARED changed" (all current
  listeners are SHARED consumers or Step0's fan-out, see below). New
  `step0_mapping_changed(channel)` for STEP0. Step0's page and the Tissue
  Preview coordinator (for the Step0 context) listen to it; Step0's fan-out
  stops listening to `mapping_changed`. The owed-redraw bookkeeping
  (`_owed_own_redraws`, step0_page.py:9766) is fed by the STEP0 signal, so a
  STEP0 write while Step0 is hidden (a restore) is still replayed on return.
  Listeners today: step1_viewer_binding 198, step1_compose_binding 111, the
  coordinator 1174 (SHARED consumers), Step0 page 8278 -- whose fan-out also
  re-emits `Step0Page.display_mapping_changed`, which MainWindow (903 ->
  `_on_display_mapping_changed` 8604) uses to drop Step1's stale remapped
  overlay pixels. That consumer is SHARED's: MainWindow connects it to
  `state.mapping_changed` directly, and Step0's fan-out (now STEP0) stops
  re-emitting into it, so a Step0 edit no longer invalidates Step1's cache.
* SEEDS (finding 4). A seed is the pixel-derived automatic window and is the
  same number for both tables. One computation; on arrival it fills EACH table
  independently where that table has no entry for the key (never over an entry,
  never marking anything user-adjusted). The sync seed port is split: a pure
  `seed_window(channel, nucleus)` (pixels only, `_seed_display_mapping(strict=
  True)`) replaces today's `seed_display_window`, which reads the workbench's
  current params. The async worker keeps its dedup key (binding, channel, role);
  routing is "fill every empty table", so no scope travels with the request.
  GPU seed requests (step1_viewer_mount.py:996) are SHARED readers and need no
  change beyond reading SHARED.
* BOTH SEED GATES ARE PER TABLE (v2 finding 5). `request_mapping_seed`
  (block01_display.py:2104) submits when ANY table lacks the key (today it
  asks SHARED only, so a populated SHARED would starve STEP0, e.g. Step0's
  computed-only Tissue Preview at step0_page.py:8206); `_on_mapping_seeded`
  (2207) checks and fills each table independently.
* ONE CANONICAL SEED (v2 finding 6): the page's slide seed
  (`_seed_display_mapping(strict=True)` -> `core.display_mapping.
  seed_display_range` over the whole-slide low-res array). The workbench's
  provider path (`_seed_range`, which also forces `hi >= lo + 1`) is no longer
  a source of record for Step0: whatever the editor seeds on pixel arrival is
  replaced by the canonical seed in every table that lacks a seeded record
  (today's reconciliation at step0_page.py:8488 generalised). A seed never
  marks a record user-adjusted and never replaces one that is.

### 3.3 The Intensity window: one editor projecting one table

THE RECORDS LIVE IN THE DATASET'S DISPLAY NAMESPACE (v2 finding 4):
`DisplayNamespace.window_records = {table: {"params": {ch: full remap dict},
"seeded": set, "user_adjusted": set, "fallback": {ch: triple}}}` beside the
triples. So the full dictionaries follow the existing `DatasetIdentity`
exactly as the triples do: a new slide binds a new namespace (empty records,
seeded afresh), A -> B -> A finds A's records again, eviction drops them with
the namespace, and the restore snapshot (deep copy) covers them. Step0's
existing clear of the workbench on a dataset switch (step0_page.py:10863)
stays; projection after it reads the NEW namespace's records.
A small stateless module `ui/step0/window_tables.py` holds the operations on
one namespace's records (get/put/export/fill-seed/flags), so the page and the
display state do not each reimplement them.

The workbench is an EDITOR of the projected table (the review's alternative,
finding 10), with no new stores inside the generic widget:

* `Step0Page._project_table(name)`: copies `WindowTables[name]` into
  `wb._params/_seeded/_user_adjusted` for the workbench's channels (missing
  entries: see the next bullet), then reloads the controls/histogram of the active channel. Called on
  `scope_changed` when `table_for_scope` changes, and after every workbench
  rebuild (`set_channel_images`, `clear_channel_images` -> re-project when
  channels return) so a rebuild cannot lose a table (finding 3).
* Missing entries at projection get the canonical seed when the pixels are
  resident, else stay provisional (not seeded); never `wb._seed_range`.
* Write-back: `params_changed` (slider, gamma, Auto, Reset, histogram) ->
  `_adopt_workbench_window` -> `WindowTables[projected][ch]` (full dict,
  user_adjusted) and `state.set_mapping(table=projected)` (DAPI: both role keys,
  as today). Pixel arrival: the workbench gains ONE generic signal,
  `pixels_installed(name)`, emitted on EVERY exit of `_install_loaded`,
  including its early returns for user-adjusted and already-seeded entries
  (channel_workbench.py:1271) -- the other table may still need its seed --
  and on both the synchronous provider path and `deliver_pixels`; `_install_loaded` itself
  emits no `params_changed`, so this is the page's only hook. The page then
  fills every table's record that is not seeded with the canonical seed (no
  user flag), updates the state triples of those tables, and re-projects the
  editor's channel if the projected record changed (v2 finding 6).
* `_display_fallback` (channels the workbench does not know) becomes the
  per-table `fallback` record in the namespace (finding 5).
* Exports, never "current": `WindowTables.export(STEP0)` for Step0 Save
  (`wb.build_config(params=...)`: one optional argument on the widget), the
  Save dirty check, `rm_save_draft`; `export(SHARED)` for
  `display_mapping_draft` (seeded entries only) and the preview provider's
  remap + `user_adjusted` metadata (provider asks for its table).

### 3.4 Who reads which table

| reader | table |
|---|---|
| Step0 full image, compare panels, Step0 Tissue Preview, Step0 engine display rows, Intensity while Step0 is shown | STEP0 |
| Step1 draft/previews, commit draft, fusion identity (precedence run -> committed -> live draft unchanged), run `intensity` record | SHARED |
| Q2a `adopt_intensity` | SHARED only (own pending dict) |
| Step2/Step3/Regions viewers, tissue contexts (main_window 445, 3196), Step1 bindings | SHARED |
| Step0 Save -> `step0_channel_remap.json` (+ handoff hash, unchanged algorithm), Save dirty check, `drafts.step0.intensity` | STEP0 |
| `drafts.shared_windows` (new, R2/R3) | SHARED |

### 3.5 Persistence, reopen, handoff

* Step0 draft: `rm_save_draft` writes STEP0's records (unchanged key
  `drafts.step0.intensity`).
* SHARED record (R2, R3; v2 findings 1-3): a NEW session key
  `drafts.shared_windows = {"edited_against": {"correct_run": <session.editing
  .correct_run>}, "params": {...}, "seeded": [...], "user_adjusted": [...]}` --
  the COMPLETE SHARED table (automatic and inherited windows included, not only
  user-adjusted ones), bound to the correct run only (windows are whole-slide
  answers; a geometry revision does not move them). It is NOT inside
  `drafts.step1`, so the existing whole-draft gate (`drafts.step1.edited_against
  == session.editing`, main_window.py:4280/4569) is untouched and cannot reject
  it. Written by `_rm_save_view` on every step change and on close, next to
  `rm_save_draft`, independently of `_rm_auto_step1` (so reopen -> Regions ->
  edit -> close keeps the edit, main_window.py:3636/4557/5508). Never written
  into the committed settings file.
* Reopen, separate sources and separate pending dicts (finding 1):
  - STEP0 <- saved remap json, then `drafts.step0.intensity`.
  - SHARED <- `drafts.shared_windows` when its correct run is the current one
    (it already contains whatever Q2a or Step1 had put there, and is never
    older than the committed file); otherwise <- the saved remap json, which is
    then exactly the handoff of the current run (R2: a new run starts Step1
    from Step0). Restored at workspace reopen together with STEP0, not on the
    deferred Step1 entry.
  - COMMITTED WINDOWS ARE RESOLVED AT REOPEN, NOT ON STEP1 ENTRY (v3 finding
    1). When SHARED came from the handoff baseline (no valid
    `drafts.shared_windows`), MainWindow reads the committed fusion settings
    right where the opened workspace's handoff is accepted (main_window.py
    ~3636, where `_rm_auto_step1` is set), with the SAME validation as
    `_restore_fusion_settings` (factored into one pure check; refused files
    are left alone, as today), and fills its `display_mapping` into SHARED --
    full dicts and triples, no user flag, only channels without a user flag.
    So the first `drafts.shared_windows` ever written (even by a close before
    Step1, or after a Regions edit) already contains the committed windows,
    and an existing project without the new key migrates correctly. The
    fusion part of Q2a (`install_committed_snapshot`) stays on Step1 entry,
    unchanged.
  - On Step1 entry, Q2a's window adoption (`adopt_intensity`, own pending
    dict) becomes fill-only and once per binding: it writes only SHARED
    channels that still hold the handoff baseline and carry no user flag; when
    SHARED came from `drafts.shared_windows`, or the reopen step above already
    adopted, it writes nothing. A delayed Q2a therefore cannot clobber SHARED
    edits made in Regions or Step1 before it runs.
  - Step1's automatic windows (`_auto_window_cache`, step0_page.py:4918) keep
    their formula and precedence (a seeded SHARED record first, else the
    automatic window); when computed they are materialised into the SHARED
    record as seeded, `auto=True`, not user-adjusted, so the complete record
    holds them.
* Handoff ordering (new correct run or first handoff only; review v2):
  compare against the OLD published run before the session binding is replaced
  (step0_page.py:11645); after a SUCCESSFUL publication and before completion is
  announced (12302): SHARED records <- copy of STEP0's, SHARED triples in one
  `adopt_mappings` transaction, `drafts.shared_windows` rebound to the new run
  on its next write, and any pending Q2a/old-run intensity retired so it cannot
  overwrite the copy. An Intensity-only Save (same run, 11859) leaves SHARED
  untouched (R2); a failed Save copies nothing.

### 3.6 Nucleus role (finding 7)

Table records are per channel; state keys are per (channel, role). Explicit
edits of DAPI write both role keys of the SAME table (as today). A
role-specific seed fills only empty keys and never replaces a channel record
that is user-adjusted. Export uses the channel record.

### 3.7 Step0 call sites that must name STEP0 (review v2 inventory)

`_display_mapping_is_real` (step0_page.py:7355, 7365: STEP0 state and STEP0
flags, never SHARED or the projected editor); `display_window` /
`set_display_mapping` reads and writes (8432, 8433, 8593); editor write-back
(8641, 8648: the PROJECTED table, both DAPI roles, `persist=False` kept); pixel
delivery (4061: the reconciliation above); Step0 engine display rows
(engine_mount.py:376, 382: controller AND nucleus-overlay windows, verified by
pixels); preview provider (preview_source_provider.py:113, 170: the caller
names its table -- Step0's correction preview asks STEP0, Step1's asks SHARED);
`tissue_render_snapshot`, compare strip, full image (STEP0).

## 4. Out of scope

Selection and ticks (already per step). Colours (process-wide). The generic
workbench's behaviour for Step1.5/Step3 hosts (one optional `build_config`
argument only).

## 5. Steps

1. Records in `DisplayNamespace` + `window_tables` operations + unit tests.
2. Display state: tables, `table=` parameter, STEP0 signal, per-table seed
   gates and canonical seed fill, restore scope forwarding; unit tests.
3. Step0 page: table-aware ports, projection/write-back, exports, Step0 call
   sites (`table=STEP0`), fallback per table, Step0 fan-out on the STEP0 signal,
   engine rows, Tissue Preview Step0 context.
4. Step1: `display_mapping_draft` -> SHARED export; Q2a own pending dict and
   its fill-only rule; `drafts.shared_windows` save (every step change/close)
   and restore at reopen (R2/R3); MainWindow cache invalidation on SHARED.
5. Handoff copy on new-run publication (R2).
6. Tests (§6), full suite, replay on the user's project copy.

## 6. Tests (real Qt, synthetic slide, never a real project)

1. Tables/state: write isolation both ways; default SHARED; seed fills each
   empty table once and never over a user entry; STEP0 signal vs
   `mapping_changed`; rollback restores both; restore with moved identity
   keeps scope.
2. Editor: projection on scope switch with Intensity left open; edits land in
   the projected table only; Auto and Reset; both DAPI roles; rebuild
   (`set_channel_images`, clear + refill) keeps both tables; pixel arrival
   while the other table is projected; dataset A -> B -> A.
3. Round trip (MainWindow): Step0 windows -> Step1 with committed settings
   (Q2a) -> Step0: Step0 unchanged, Step1 shows its saved windows; Step1 edit ->
   Step0 unchanged; Step0 edit -> Step1 draft, full fusion param dict hash and a
   frozen run's inputs unchanged; viewer and Tissue Preview PIXELS for both.
4. Save: Intensity-only Save leaves SHARED; new-run Save copies; failed Save
   copies nothing; handoff hash follows STEP0 only.
5. Reopen: independent unsaved Step0 and Step1 edits with no Step1 commit both
   come back in their own tables; SHARED record of another correct run is
   ignored (SHARED from the handoff); Q2a precedence (adopts only into a
   handoff-baseline SHARED, never over the record or a session edit; delayed
   Q2a vs a live SHARED edit).
5a. R2 across restart: first handoff -> Step1 never adjusts or commits a
   channel -> Step0 changes it + Intensity-only Save -> restart: SHARED keeps
   the handoff window.
5b. Regions: reopen -> Regions -> edit Intensity -> close -> reopen (edit kept);
   and reopen -> Regions -> edit -> enter Step1 (edit kept, not replaced by the
   deferred restore).
5c. Seeds: one table populated and the other empty, both directions (request
   gate and result gate); float slide with values in 0.2..0.8 (canonical seed,
   no `+1` clamp); pixel arrival before and after projection; arrival while the
   projected table holds a user edit.
5d. Step0 engine pixels (controller and nucleus overlay) follow STEP0.
5e. Migration (synthetic project with VALID committed fusion settings and NO
   `shared_windows` key): open -> close before Step1 -> reopen -> Step1; and
   open -> Regions -> edit one channel -> close -> reopen -> Step1. Untouched
   channels show their committed windows, the Regions edit survives, STEP0 is
   independent, the committed file is byte-identical.
6. Step2/Step3/Regions keep sharing SHARED with Step1.
7. Existing suites, then the full suite (shared-code change); replay
   `make_scen_user.py` on the user's project copy: Step0 windows identical
   before and after the Step1 round trip.

## 7. Risks

* Direct `wb._params` writers outside the editor path (step0_page 8504, 8613,
  `_apply_workspace_intensity`) must go through `WindowTables` and re-project.
* A seed result landing after a dataset switch: unchanged (binding checks).
* Effort: about 3 days including tests.

## 8. Review findings (v1) and where v2/v3 answer them

| finding | answer |
|---|---|
| 1 reopen leaks Step0 draft into shared; shared pending dict | §3.5 separate sources and pending dicts |
| 2 Step1 draft persistence | R3 + §3.5 `drafts.shared_windows` with its own binding |
| 3 workbench rebuild/clear/pixel arrival per table | §3.3 tables outside the widget, projection after rebuild, fill every empty table |
| 4 seed routing sync/async | §3.2 pure seed, fill-each-empty rule |
| 5 missed readers, fallback, owed redraws, table vs scope | §2/§3.4 inventory, explicit table, STEP0 signal feeds owed redraws, `table_for_scope` |
| 6 Q1 same-run Save / Q2 hash | R2; hash unchanged over STEP0's remap json |
| 7 nucleus role, full dicts, precedence | §3.6, full dicts in `WindowTables`, precedence unchanged |
| 8 restore scope propagation | §3.2 |
| 9 tests | §6 |
| 10 simpler model | adopted: tables outside, workbench as editor |

## 9. Review findings (v2) and where v3 answers them

| finding | answer |
|---|---|
| 1 R2 broken across restart (SHARED rebuilt from Step0's newer remap json) | §3.5 complete SHARED record `drafts.shared_windows`, bound to the correct run; test 5a |
| 2 `edited_against` schema conflict | §3.5 separate key with its own correct-run binding; `drafts.step1` untouched |
| 3 Regions edits lost before deferred Step1 restore | §3.5 written by `_rm_save_view` regardless of `_rm_auto_step1`, restored at reopen; Q2a never over the record or a session edit; test 5b |
| 4 records' dataset ownership | §3.3 records inside `DisplayNamespace` |
| 5 seed request gate checks SHARED only | §3.2 both gates per table; test 5c |
| 6 two seed formulas; no write-back on pixel arrival | §3.2 canonical seed; §3.3 `pixels_installed` hook; test 5c |
| inventory | §3.7 |
| handoff ordering | §3.5 |

## 10. Review findings (v3) and where v4 answers them

| finding | answer |
|---|---|
| 1 first `shared_windows` save suppresses committed windows (migration) | §3.5 committed windows resolved at reopen (same validation), Step1-entry Q2a fill-only once per binding; test 5e |
| stale `wb._seed_range` bullet | removed (§3.3) |
| `_auto_window_cache` outside the record | §3.5 materialised into SHARED as seeded/auto |
| `pixels_installed` and early returns | §3.3 every exit |

## 11. Implementation (2026-10-27)

v4 was APPROVED (codex_s0ranges_plan_4.out). Implemented as planned, with these
deviations, each for the reason given:

* Records shape: `{"params", "user_adjusted"}` only. `params` holds REAL windows
  (seeded or typed), so "seeded" is "present" and the per-table `fallback`
  merges into `params` (a channel the editor does not hold is a record like
  any other). `core/display_identity.empty_window_record`.
* Step1's automatic windows (`_auto_window_cache`) are NOT materialised into
  the SHARED record (§3.5 said they would be): they are a deterministic
  function of the same correct run's pixels, so recomputing them gives the
  same numbers, and materialising them would change what Step1's viewer
  (which seeds through the canonical seed) draws today. The SHARED record
  holds every window the shared table actually holds.
* The canonical seed (`_seed_display_mapping`) also reads the Intensity
  window's own resident pixels when the slide's low-res array is not
  resident: they are the same whole-slide overview read, and without it a
  channel the window has loaded stayed provisional where today it was real.
* Restoring a workspace's two tables is one method,
  `Step0Page._restore_workspace_windows(ws, draft)`, called by the workspace
  restore and by tests.

Code: core/display_identity.py (tables, records), ui/block01_display.py (state
routing, `fill_seed`, `missing_tables`, `replace_mappings`, STEP0 signal,
per-table seed gates, coordinator per table), ui/step0/window_tables.py (new),
ui/step0/step0_page.py (ports, projection, exports, reopen, Q2a fill-only,
`drafts.shared_windows`, handoff copy), ui/step0/preview_source_provider.py,
ui/widgets/channel_workbench.py (`pixels_installed`, `build_config(params=)`),
ui/main_window.py (SHARED cache wiring, record on every step change/close,
committed windows at reopen, validation factored out of the restore).

Tests: test_step0_own_windows_state.py (9), _page.py (16), _walk.py (5); old
tests that asserted the one-table design updated to the ruling
(test_step0_display_mapping, test_step1_display_mapping_commit,
test_block01_tissue_preview_contract).
