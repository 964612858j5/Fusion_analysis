# Cores found without a map sheet: only the cores are analysed (backlog 11, stages 2-3) — plan v0.2

User (TG) 2026-11-11: "没有地图表，只要找到了cores，那我认为不应该继续整张切片处理，而是应该只处理cores的范围" (stages 2-3 of `docs/v17_find_cores_without_map_plan.md`); "阶段 2 没有地图表时，我认为不需要对cores命名"; "有地图表时维持现状". Stage 1 (58c9dfa) lets Find cores run without a map sheet; downstream still treats such a slide as one region. After stage 1 the user noticed: "当前似乎没有地图表也能save进入下一步" -- yes, and the slide is then processed whole; this plan changes that.

## 1. Today (read-only; full list from a code search, scratchpad notes)

* **The snapshot** (`core/regions/analysis.py`): `mode = "tma"` if the document has a map sheet or a study exists, else `"roi"` (:194). Only `"tma"` fills `cores` (via `select_cores`, which walks the MAP SHEET's positions: Run tick `doc.positions[key].run`, binding by `position_key`, `mapping == "matched"`, QC per position) and the `positions` table. `"roi"` = the parent region minus exclusions, no cores. `identity` (:273) hashes each core's `position_key`, `donor_block_id`, `tma_unique_sample_id`, `annotation`, `qc`, `polygon` (`KeyError` if absent).
* **The Run tick exists only per map position** (`document.py` `_set_positions`, `set_run(s)`). A core record is `{pieces, position_key, custom_label, mapping}`; nothing per core can be ticked. The Cores tab lists map-less cores with no checkbox.
* **Raster / object rules** (shared by Step1 pre-seg, Step2, Step4): `AnalysisRaster.tma = mode == "tma"` (`raster.py:96`); not `tma` → the parent region is the only "core" and `cores` are ignored (:115). `ObjectTally(tma=...)`, `preseg_input.regions_filter`, Step2's judge follow it.
* **Step1** (`ui/main_window.py`): `_preseg_regions` (:6927) builds snapshots per input kind; omitted cores are printed by `label` (:6970); `_random_patch_cores` (:7035): not `tma` → random patches over the whole tissue.
* **Step2** (`main_window._step2_manual_regions`, :2227): without a study, a Save **without exclusions gives no snapshot at all** (the old, unrestricted run, :2263-2269); `NothingTicked` → ask, only for TMA.
* **Step4** (`core/regions/quant_apply.py`): `RegionsInput.tma` (:69); `core_areas` keyed by `position_key` (:131); `load_regions` refuses a non-`tma` snapshot whose objects have cores (:186); `obs_frame` writes, only for `tma`, `core_position_key`, `core_position_id` (int64), `core_label`, donor / sample / annotation / block / project / height / QC columns, `core_area_*`, `cell_core_fraction`, `cell_touches_core_edge`, `analysis_include` (:281-320); `tma_cores_frame` is one row per MAP position (:323); `provenance_summary` lists `c["position_key"]` (:373). `feature_extract_worker` writes these to h5ad / CSV; `uns["tma_cores"]` only for `tma`. SpatialData shapes already use `uid` (`position_key or ""`).
* **The Save** (`core/regions/lock.py`): core regions and `tma_cores.parquet` only with a map sheet (:225); `rec["run"]` = ticked position keys.

So "only the cores" for a map-less slide touches the snapshot, the raster rule, the Regions Run tick, Step1, Step2's hand-over and Step4's tables -- every consumer reads the same snapshot.

## 2. Decisions for the user

* **Q1 — one block, not two.** One snapshot feeds every step, by two routes: a pre-segmentation result chosen with Use carries Step1's frozen snapshot to Step2 (`core/preseg_contract.py:104-114`, checked by `RA.identity` in `segment_merge_worker.py:811`), and a manual Step2 run freezes its own from the last Save (`main_window._step2_manual_regions`, which today gives a map-less Save without exclusions no snapshot at all). Step4 then reads the run's snapshot (`quant_apply`, which refuses a non-TMA snapshot with core-assigned objects). Releasing Step1 alone would leave the two Step2 routes disagreeing and Step4 refusing. **Recommended: stages 2 and 3 as ONE block** (one plan, one commit, one acceptance through Step4), implemented in order snapshot → Step1 → Step2 → Step4.
* **Q2 — which cores run.** Each found core gets a **Run tick in the Cores tab, ticked by default**; untick to leave one out. Pieces not in a core and dropped pieces never run (as with a map sheet). (Asked before as Q3 of stage 1; not answered yet.)
* **Q3 — no names, but an identifier in the results.** Per the ruling, no C1…Cn. The tables still need to say which core a cell is in: the core's internal id (`core_…`, stable across edits and Saves) plus its centre and area. In the viewer the outlines carry no text.
* **Q4 — no per-core QC without a map sheet.** QC (accepted / excluded + reasons) lives on map positions; for map-less cores, unticking Run is the way to leave one out. (Adding per-core QC is a later block if wanted.)
* **Q5 — unchanged:** a slide with a map sheet (numbered, ticked cores only); a slide without a map sheet and **no** found cores (whole region, exclusions); with a map sheet chosen, unnumbered cores stay out (as today).

The rest assumes Q1-Q4 as recommended.

## 3. Design

### 3.1 The snapshot: a third mode, `"cores"`
* `build_snapshot`: no map sheet and no study, and the Save's document has at least one core → **mode `"cores"`**. Otherwise as today (`"tma"` / `"roi"`).
* `select_cores_without_map(doc, select)`: every core of the document with `run` true (§3.2), an outline with area, not all pieces dropped, inside the workspace's parent region (the same raster probe as `"tma"`); the rest are `omitted` with a reason (`not_ticked`, `degenerate`, `outside_region`). Overlapping selected cores are refused, named by their centre ("core at (y, x)"). Nothing ticked → `NothingTicked`, as `"tma"`.
* Each selected core: `uid`, `polygon` (the same geometry helper as `"tma"`), `region_id` computed as for `"tma"` (`region_id(..., "TMA_core", ...)`, `analysis.py:127`), `centre_yx`, `position_key None`, `label ""`, `donor_block_id ""`, `tma_unique_sample_id ""`, `annotation ""`, `core_height ""`, `qc "unreviewed"` -- every key present, so `identity` and readers that index it keep working. `identity` adds `uid` to a core's fields **only in `"cores"` mode** (a `"tma"` snapshot's identity is byte-identical to today's).
* `positions` stays `[]`, `map_sheet None`.
* **A frozen inventory of every saved core** (`core_inventory`, `"cores"` mode only): one record per core of the Save, keyed by uid -- centre, area (0 for a degenerate outline), run tick, and its reason (`""` selected, `not_ticked`, `degenerate`, `outside_region`, `overlap` is a refusal, not a reason). `omitted` records are uid-keyed too (`uid`, `centre_yx`, `reason`). This is what Step4's per-core table and the messages use, so a core with a bad outline or zero cells still has its row.
* `display_cores` (`core/regions/display.py`): in `"cores"` mode the reason of an unselected core comes from the inventory (not `not_bound`); a degenerate outline stays out of the drawing (as today) but stays in the inventory.

### 3.2 The Run tick per core (Regions)
* A core record gains `run` (default **True**) when it is made without a map sheet (`attach_candidates`, a drawn / merged core with no map sheet); an older document without the field reads as True. `set_core_run(uid, on)` / `set_core_runs` (one undo step), mirroring `set_run(s)`.
* The Cores tab, without a map sheet: each core row has the Run checkbox and Run all / none, as the position rows do with a map sheet. With a map sheet nothing changes (the field is ignored there; `attach_map` drops it).
* The Save (`lock.py`): `rec["run"]` stays the ticked positions; for a map-less document it also records the ticked core uids (`run_cores`) so the Save says what will run. No `tma_cores.parquet` (position-based) is written for `"cores"`. The document's content hash, deep-copy undo / restore and `table_matches` already carry a new core field; nothing new is needed there.
* Lifecycle rules: a merge (Group / Union) keeps the receiving core's record, so its `run` -- the result runs if the receiving core was ticked (stated, tested with mixed ticks); a core split off / drawn without a map sheet starts ticked; undo / redo, Save → reopen, Revert to a Save and a locked (read-only) version keep the ticks; in an ROI workspace the ticks are read-only (cores are edited in the whole-slide workspace, as today); Run all / none act on the rows the table shows; `attach_map` drops `run` and its undo brings the ticks back.

### 3.3 Raster and object rules
* `AnalysisRaster.tma`, `ObjectTally(tma=...)`, `preseg_input.regions_filter` and Step2's judge treat `"cores"` like `"tma"` (cores rasterised from their polygons; an object outside every selected core is dropped `no_core`). One helper `has_cores(snapshot)` = mode in ("tma", "cores").

### 3.4 Step1
* `_preseg_regions`: `"cores"` snapshots per input kind like `"tma"`; omitted cores printed by centre, not label.
* `_random_patch_cores`: `"cores"` → random patches inside the selected cores (like `"tma"`). When the Save has map-less cores but the snapshot fails (none ticked, overlapping ticked cores, none eligible inside the workspace), random patches are **refused with the reason** -- today's fallback to the whole tissue (`main_window.py:7053-7059`, errors swallowed when there is no study) applies only to a slide with no cores at all.
* `_compute_step1_core_items`: unchanged (the snapshot's `display_cores` draw them; no names).

### 3.5 Step2
* `_step2_manual_regions`: no study, and the Save has cores → a `"cores"` snapshot is frozen (whatever the exclusions), `NothingTicked` → the same question as TMA ("analyse every eligible core?"). No cores → today's behaviour.
* `segment_merge_worker`: the run metadata gets `core_uids` in `"cores"` mode (`position_keys` unchanged for `"tma"`); the rest is index-based already.
* `core/provenance.py`: the three records that list cores -- the contract-based segmentation run (:512), the manual Step2 run (:528) and Step4 (:587) -- record `core_uids` in `"cores"` mode; TMA records unchanged.

### 3.6 Step4
* `RegionsInput.tma` → true for `"cores"` too (`has_cores`); `load_regions`'s rule follows.
* `core_areas` keyed by core `uid` (both modes; `"tma"` output unchanged because its columns still read by position key through a uid → key map).
* `obs_frame` in `"cores"` mode writes: `core_uid`, `core_region_id`, `core_centre_y`, `core_centre_x`, `core_area_px` / `_um2` / `_mm2`, `cell_core_fraction`, `cell_touches_core_edge`, `analysis_include`. No position / donor / sample / block / project / height / QC columns (they do not exist). `"tma"` mode: unchanged, plus nothing.
* `uns["cores"]` (new, `"cores"` mode only): one row per core of the snapshot's `core_inventory` -- uid, centre, area, run, reason, `n_obs` (0 for a core with no cells). `uns["tma_cores"]` stays TMA-only.
* `provenance_summary`: `core_uids` in `"cores"` mode; `position_keys` unchanged for `"tma"`.
* CSV: the same columns as obs.

Not changed: the map-sheet path end to end, `"roi"`, Regions' drawing, Step3, the segmentation engines.

## 4. Files (closed list)
* `core/regions/analysis.py`, `core/regions/document.py`, `core/regions/lock.py`, `core/regions/raster.py`, `core/regions/objects.py`, `core/regions/quant_apply.py`, `core/regions/display.py`, `core/preseg_input.py`, `core/provenance.py`.
* `ui/regions/cores_panel.py`, `ui/regions/cores_actions.py` (Run tick for map-less cores).
* `ui/main_window.py` (`_preseg_regions`, `_random_patch_cores`, `_step2_manual_regions`).
* `workers/segment_merge_worker.py` (`_regions_run_meta`), `workers/feature_extract_worker.py` (uns `cores`).
* Tests (new): `tests/test_cores_mode_snapshot.py`, `tests/test_cores_mode_step2_step4.py`; existing TMA / roi tests must pass unchanged.
* This plan.

## 5. Tests (synthetic project)
1. Snapshot: map-less Save with 4 cores, one unticked, one outside the ROI workspace → `"cores"`, 2 selected, 2 omitted with reasons; overlapping ticked cores refused by centre; none ticked → `NothingTicked`; no cores → `"roi"` as today; with a map sheet → `"tma"` and its `identity` equal to HEAD's on the same Save.
2. Run tick: default ticked; untick / Run all / none, undo; an old document without `run` reads ticked; `attach_map` drops the field.
3. Raster: an object outside every selected core is dropped `no_core`; inside → its core index.
4. Step1: pre-seg input and random patches stay inside the ticked cores; random patches are refused (with the reason) after Run none, with overlapping ticked cores, and with no eligible core in the workspace; a slide with no cores still gets whole-tissue patches.
5. Step2, both routes: **Use → Generate → Step2** (the contract's frozen `"cores"` snapshot passes the worker's identity check) and **Generate without Use → manual Step2** (a map-less Save without exclusions freezes a `"cores"` snapshot); the run metadata and the registered provenance list `core_uids`; nothing ticked → the question.
6. Step4: obs / CSV columns of §3.6, per-core areas by uid, `uns["cores"]` with a row for every saved core (also degenerate, unticked, outside, and one with zero cells), registered Step4 provenance `core_uids`.
6a. TMA unchanged (golden): on the same synthetic TMA with omitted and QC-excluded positions, the snapshot `identity`, Step4's obs / CSV columns (order, dtypes, values), core areas and `uns["tma_cores"]` equal HEAD's.
6b. Run-tick lifecycle: mixed ticks merged by Group and by Union (the receiving core's tick), undo / redo, Save → reopen, Revert, a locked version, an ROI workspace (read-only), Run all / none on the shown rows, `attach_map` and its undo (ticks back).
6c. Compatibility: a `"cores"` snapshot checked by HEAD's `identity` does not match (so an older build refuses it as changed, not misreads it).
7. Existing Regions / TMA / Step2 / Step4 tests: no new failures against HEAD.

Acceptance (user, Mac): the demo TMA without a map sheet: Find cores, untick one, Save → Step1 pre-seg and Step2 work inside the three ticked cores only; Step4's table has `core_uid` / area columns and no cells outside them. Then with the DEMO map sheet: as before.

## 6. Risks and rollback
* The snapshot gains a mode; old snapshots (`"tma"`/`"roi"`) read as before. An older build does not check the mode (`load_snapshot` checks version and identity), but its `identity` of a `"cores"` snapshot differs (no `uid` in its fields), so it refuses the snapshot as changed rather than misreading it (tested, §5.6c).
* Rollback: revert the commit; map-less slides go back to whole-region processing.

## 7. Plan review (codex astra low)

Round 1 FAIL (scratchpad `codex_g11s2_plan_r1.txt`), all taken in v0.2:
* P2: random patches fell back to the whole tissue when a map-less snapshot failed. §3.4 refuses with the reason; tests §5.4.
* P2: provenance records listed only `position_keys`. `core/provenance.py` added (three records get `core_uids`); tests check the registered records.
* P2: a per-core table for every saved core had no frozen data. §3.1: uid-keyed `core_inventory` and omitted records; `display.py` reasons; tests with degenerate / unticked / outside / zero-cell cores.
* P3: Q1's reasoning now names both Step2 routes; both are tested.
* P3: Run-tick lifecycle stated (§3.2) and tested (§5.6b).
* P3: `region_id` computed as for TMA; the compatibility claim corrected (identity, not mode) and tested.

Round 2 PASS (scratchpad `codex_g11s2_plan_r2.txt`), no findings.

## 8. Implementation (uncommitted, 2026-11-11)

User (TG): Q1-Q3 agreed; Q4 explained (QC here is the whole-core
accept / exclude with reasons that exists only on map positions, not the
blur / fold exclusions, which keep working) and agreed; "可以开始了".

* `core/regions/analysis.py`: `CORE_MODES`, `has_cores`, `core_where`,
  `select_cores_without_map` (selected, uid-keyed omitted, `core_inventory`),
  the `"cores"` mode in `build_snapshot` (refusals as TMA, overlap named by
  centre), `identity` adds `uid` only in `"cores"` mode.
* `core/regions/display.py`: `display_cores(..., why_uid=)`.
* `core/regions/raster.py`: `tma` true for `"cores"` (ObjectTally,
  pre-seg filter and Step2's judge follow it).
* `core/regions/document.py`: `new_core` gives a map-less core `run: True`;
  `set_core_run(s)`, `core_runs`; `attach_map` and `attach_tma` drop `run`
  (attach_tma builds its cores before the map sheet is set, so without
  this a TMA core would have gained the field).
* `core/regions/lock.py`: `draft_from_locked` takes a map-less version's
  cores (and ticks) from its document; a map-less Save records `run_cores`.
* `ui/regions/cores_panel.py` / `cores_actions.py`: the Run tick on
  map-less core rows (no study only), routed to `set_core_run(s)`.
* `ui/main_window.py`: `_random_patch_cores` refuses when map-less cores
  cannot run; `_step2_manual_regions` freezes a `"cores"` snapshot for a
  map-less Save with cores (with or without exclusions), the same
  nothing-ticked question.
* `workers/segment_merge_worker.py`, `core/provenance.py`: `core_uids` in
  `"cores"` mode (run metadata, the three provenance records).
* `core/regions/quant_apply.py`: `has_cores` beside `tma`,
  `core_areas_by_uid`, the `"cores"` obs columns, `cores_frame` (a
  degenerate core keeps area 0), `core_uids` in the provenance summary;
  `workers/feature_extract_worker.py`: `uns["cores"]`.
* Tests: `tests/test_cores_mode_snapshot.py` (9),
  `tests/test_cores_mode_step2_step4.py` (7). Stage 1's
  `test_regions_find_cores_without_map.py::test_a_save_without_a_map_sheet_analyses_the_region`
  asserted the `"roi"` mode this plan replaces: renamed
  `..._analyses_its_cores`, now asserts `"cores"` and the Run tick.

TMA unchanged, checked against HEAD in a separate worktree: a TMA Step4
run's obs columns, dtypes and every value, `uns["tma_cores"]`, the uns
keys, the provenance (paths aside) and the CSV header are identical; the
TMA snapshot's identity equals the old formula (test).

Code review (codex astra low) round 1: no P1/P2; P3s taken -- a
degenerate core keeps area 0 in `uns["cores"]` (test with the real
builder); lifecycle tests for Union, undo / redo and the undo of
`attach_map`. Not added: UI tests for filtered Run all / none and the ROI
workspace's read-only ticks (the table and the read-only flag are the
existing ones used with a map sheet).

Regression (one process per test; 35 Regions / TMA / F4 / F5 / Step2 /
Step4 / quant files, against a HEAD worktree): 587 tests on the change,
573 on HEAD; no test fails on the change that passes on HEAD (one timing
test failed on HEAD only). The Step4 files rerun on the final code: same.
Code review round 2 (scratchpad `codex_g11s2_code_r2.txt`): PASS.

Acceptance (user, Mac, 2026-11-11): the demo TMA without a map sheet, 4
cores found, 3 ticked, Step0 → Step4. Checked in the output
(`out1/.../quant_20261111_223027/Full_WSI`): mode `cores`, 24 732 cells
all in the 3 ticked cores (9 394 / 9 365 / 5 973), every centroid inside
its own core's outline, none near the unticked core; `uns["cores"]` lists
all 4 (the unticked one `not_ticked`, 0 cells, its area kept); no
map-sheet columns; provenance `core_uids` = the 3 run cores; 4 303 objects
dropped by Step2 as `no_core`. User: "只要输出没问题那么人工验收就可以通过，可以提交推送".
