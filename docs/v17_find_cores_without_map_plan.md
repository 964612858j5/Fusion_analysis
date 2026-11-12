# TMA cores without the map sheet, and only the cores analysed (backlog 11) — plan v1.2

Started by the user (TG) 2026-11-11: today Step 0.5 has to load the TMA
information file (the map sheet) before Find cores can run; that is not
reasonable: Find cores should run whether or not there is a map sheet.

Widened by the user the same day, after plan v0.3 (codex PASS):
* "Why must Find cores have a diameter? Cores can be found without one."
* "Without a map sheet, once cores are found, the slide should not be
  processed as a whole any more: only the cores' areas. Even with a map
  sheet, only the found cores' areas. I know this touches Steps 1, 2 and 4;
  we can do it in steps."

User's answers on v1.0 (TG, same day): no diameter field, fully automatic
("a rough segmentation, e.g. watershed, of all cores and fragments, then
their median as the diameter"); stage 1 agreed; in stage 2 cores without a
map sheet get no names; with a map sheet the rule stays as it is.

So the block has three stages, each its own approval, commit and acceptance.
v0.3's design (Find cores without a map sheet; attach the map sheet later;
numbering follows the document's map sheet) is stage 1's core and is kept
below (v0.3 itself: scratchpad `find_cores_plan_v0.3.md`).

## 1. Today (read-only)

### 1.1 Find cores and the map sheet
* `ui/regions/cores_panel.py:115` `sync_enabled`: `Find cores` is enabled
  only when a study (map sheet) exists, no detection runs, and the
  workspace is the whole slide.
* `ui/regions/cores_actions.py:213` `find_cores`: refuses without a study.
  From the study it takes only the core diameter; when the sheet has none it
  asks for it (mm; level-0 px when the slide has no pixel size).
* The detection (`core/tma/dearray.py detect_cores`, run by
  `ui/tma/workers.py DetectWorker`) does not use the map sheet.
* `_detect_done` → `core/regions/document.py:57 attach_tma(d, study,
  candidates)`: sets `map_sha256`, one position per map-sheet position,
  every candidate a piece, the large ones a core ("unmatched").
* Numbering is allowed whenever a study exists (`cores_panel.py:267`);
  binding writes into `d["positions"]`, which only a document with the map
  sheet attached has.

### 1.2 Why the diameter
In `dearray`, the expected core diameter is the scale of every step (all
`PARAMS` are fractions of it):
* which pyramid level is read (`choose_level`: the coarsest where a core is
  still 80 px wide);
* the texture window, the smoothing, the closing and opening radii of the
  foreground;
* the smallest piece kept (1 % of a core's area);
* when touching cores are split (a piece of more than 1.6 cores, or 1.15
  and not convex) and how far apart the split seeds are;
* how far outlines grow (6 % of a diameter);
* the measures (tissue fraction of the expected disc).

So it is needed as a scale, not as the answer: the cores themselves are
found from the image. The scale can be estimated from the image (§3.1).

### 1.3 What is analysed downstream
`core/regions/analysis.py` builds the analysis snapshot that Step1 / Step2
/ Step4 use:
* **with a study (map sheet)**: mode `tma`; only cores that are numbered
  (confirmed), ticked Run, not QC-excluded and inside the workspace are
  analysed, each only within its outline; nothing else of the slide is.
  This already is "only the found cores", restricted to the numbered and
  ticked ones.
* **without a study**: mode `roi`: the whole workspace region is analysed,
  minus the exclusions; found cores (if any) are not used.

Code that reads the map-sheet fields (`position_key`, `donor_block_id`)
outside Regions / TMA: 9 places, in `core/regions/raster.py`,
`quant_apply.py`, `ui/main_window.py` and the analysis snapshot.

## 2. Stages

| Stage | What | Where |
|---|---|---|
| **1** | Find cores without a map sheet and without a diameter; edit, Save; attach the map sheet later | Regions and the core detection (+ two message texts) |
| **2** | Without a map sheet, the found cores (not the whole slide) are what Step1 analyses; no names shown | the analysis snapshot, Step1 |
| **3** | The same for Step2 (segmentation per core) and Step4 (quantification and export per core) | Step2, Step4, their result files |

With a map sheet nothing changes in stages 2-3: only numbered, ticked cores
are analysed, as today (§1.3). See Q2.

This plan details stage 1. Stages 2-3 get their own plan revision after
stage 1 is accepted, starting from a read of the 9 places of §1.3.

## 3. Stage 1 design

### 3.1 The diameter is found automatically (no field)
* Find cores passes no diameter: `detect_cores` **estimates** the core
  size from the image, then runs the existing detection with it. The map
  sheet's diameter, when there is one, is no longer used for detection
  (only shown next to the estimate for comparison). `detect_cores` keeps
  its `core_diameter_px` / `core_diameter_um` keywords for scripts and
  tests (then `core_diameter_source: "given"`); no part of the interface
  passes them. Without them it estimates, where today it raises.
* The estimate, as the user proposed: a rough segmentation of everything
  on the slide, then a median. Every number is fixed here, so the result
  is reproducible:
  1. **Sampling.** The level read is the coarsest whose short side is at
     least 1024 px; when no level is (a small image), level 0. If that
     level's short side is above 2048 px (a slide without a pyramid), it is
     reduced by block means with the smallest integer factor that brings
     the short side to 2048 or below. So the estimate never reads more than
     about 2048 px on the short side.
  2. **Foreground** (no core size needed): DAPI → `log1p`, Gaussian
     σ = 1 sampling px, Otsu threshold, holes filled. Specks smaller than
     **20 sampling px** in area are dropped (an absolute size at the
     sampling resolution, not a share of the image: a share would drop
     every core on a large sparse slide).
  3. **Watershed** to split touching cores: the Euclidean distance
     transform of the foreground; markers are its **h-maxima per connected
     piece, with h = 0.3 × that piece's largest distance** (h-maxima
     measure a peak's height above the saddle that joins it to a higher
     one, so two cores joined by a neck narrower than 70 % of their radius
     split, a single round core with a ragged edge does not); a plateau
     counts as one marker; `watershed(-distance, markers, mask=foreground)`.
  4. **Area-weighted median**: the pieces' equivalent diameters
     (2·√(area/π)), sorted, each weighted by its area; the estimate is the
     first diameter where the cumulative area reaches half the total
     (deterministic on ties: the smaller one). Fragments take part, each by
     its area, so they do not outvote the cores.
  5. Converted to level-0 pixels by the level's scale (and the block
     factor). Then the detection runs as today with that diameter.
* Nothing found (a blank slide): "No tissue found on the DAPI channel"; the
  cores can still be drawn by hand (Regions' existing tools).
* The candidate record keeps `core_diameter_px` and adds
  `core_diameter_source: "estimated"`.
* In Regions: Find cores runs straight away, with no question and no field.
  The info line names the result, e.g. "Candidates v3: 6 found (core size
  estimated 0.62 mm; map sheet: 0.6 mm)".

### 3.2 Find cores without a map sheet (v0.3)
1. **Find cores** is enabled in a whole-slide workspace with a project and a
   channel named DAPI, with or without a map sheet.
2. **Into the document without a map sheet**: a new
   `document.attach_candidates(d, candidates)` does what `attach_tma` does
   for the pieces and cores, but leaves `map_sha256` and `positions` empty.
   `attach_tma` calls it for its piece part. One undo step.
3. **Choosing the map sheet after the cores were found**: after the study is
   written, a new `document.attach_map(d, study)` (one undo step) sets
   `map_sha256` and the positions; the pieces and cores are kept, all
   unnumbered.
   * `attach_map` keeps every piece and core id, outline, owner and
     `dropped` mark, the exclusions, `piece_counter`, `candidates_version`
     and `core_diameter_px`; cores keep a custom label; every core's
     `mapping` becomes "unmatched" with no position. It records one
     operation (`kind: attach_map`). It refuses a study of another slide.
4. **The document's map sheet is what numbering follows**, not the study
   alone. After Undo of the attach, Revert to a Save made before it, or a
   version from before it:
   * the button next to Find cores reads `Attach map sheet` (enabled in an
     editable whole-slide draft with cores) and attaches the existing study,
     one undo step;
   * `numbering_allowed` and Annotate's blocker require the document's map
     sheet ("The map sheet is not attached to these cores yet: click
     Attach map sheet");
   * the numbering panel (line / click numbering, single-core `Number` /
     `Custom`) is enabled by `numbering_allowed()`, and `bind_single`
     refuses with the same text.
5. **A later Find cores** with existing cores asks before replacing them, as
   today.
6. **The Cores tab without a map sheet** lists the found cores (unnumbered
   rows); the line says "N cores found. Choose the map sheet to number
   them."
7. **Save without a map sheet** keeps the document; no core regions and no
   core table. Downstream in stage 1: as today without a map sheet (the
   whole region; stage 2 changes this). The snapshot's analysed `cores`
   are empty; its drawing-only `display_cores` show the found cores;
   exclusions are not linked to cores.
8. **Two messages** that read "map chosen but no map in the document" as
   "cores not found" now also cover found-but-not-attached cores
   (`lock.py:214` open issue; `analysis.py:198`). Text only.

Not changed in stage 1: the detection steps after the estimate, the
map-sheet parser, the study file, numbering rules, Step1/2/4.

### 3.3 Files (stage 1, closed list)
* `core/tma/dearray.py`: `estimate_core_diameter`; `detect_cores`
  estimates when no diameter is given; `core_diameter_source` in the
  record.
* `ui/tma/workers.py`: `DetectWorker` reports the estimate failure apart
  from other failures.
* `ui/regions/cores_panel.py`: `sync_enabled` (both buttons and the
  numbering panel), `numbering_allowed`, `refresh`.
* `ui/regions/cores_actions.py`: `find_cores` (no diameter question),
  `_detect_done`,
  `_detect_failed`, `choose_map` (and `Attach map sheet`), `bind_single`.
* `ui/regions/page.py`: `annotate_blocker`.
* `core/regions/document.py`: `attach_candidates`, `attach_map`;
  `attach_tma` uses `attach_candidates`.
* `core/regions/lock.py:214`, `core/regions/analysis.py:198`: text only.
* Tests (new): `tests/test_regions_find_cores_without_map.py`,
  `tests/test_tma_core_size_estimate.py`.
* Tests (existing, focused):
  `tests/test_tma_f2_dearray.py::test_the_expected_size_needs_a_diameter`
  becomes "without a diameter the size is estimated" (same slide; it now
  finds the cores and records `estimated`). The other tests there and
  `tests/test_regions_r1.py` pass diameters and keep their geometry and
  persistence checks unchanged.
* This plan.

### 3.4 Tests (stage 1; synthetic, offscreen)
1. Estimate, synthetic TMAs (grids of noisy discs with a DAPI-like
   texture); in each the estimate is within 15 % of the true diameter and
   the detection with it finds every disc:
   * diameters 300, 800 and 2000 level-0 px, with 1, 3 and 5 levels;
   * one core alone, two cores;
   * two pairs of touching discs (equal, and 1 : 0.7 in diameter);
   * fragments holding 10 % and 30 % of the tissue area (more fragments
     than cores in both);
   * a sparse large canvas: 4 cores of 2000 px on 60 000 × 60 000 px
     (each core under 0.1 % of the image), pyramid of 6 levels;
   * a small image without a pyramid (600 × 800) and a large one without a
     pyramid (12 000 × 9 000): the sampled short side stays ≤ 2048.
   A blank image → "No tissue found". The real demo TMA (backlog 10): 4
   cores found, the estimate within 15 % of their measured size.
2. Find cores enabled without a map sheet (whole slide, DAPI); disabled in
   an ROI workspace and without DAPI.
3. No map sheet, no diameter: Find cores on a synthetic 4-disc slide → 4
   cores drawn, no dialog, the info line names the estimate, `map_sha256`
   empty; Undo removes them in one step. With a map sheet stating a
   diameter, the estimate is still used and both are named.
4. Then Choose map sheet (synthetic 2×2 workbook) → the §3.2.3 invariants;
   numbering works; the Save has its core table. Also after a drawn piece,
   a deleted piece, a merge and a refused second Find cores; then Save,
   reopen, draw another piece (its id follows `piece_counter`).
5. Undo of the attach → no map sheet in the document; numbering refused,
   panel disabled, single-core `Number` refused; `Attach map sheet`
   attaches again; Redo works; Revert to a Save before the attach → the
   same.
6. A map sheet first, then Find cores → today's document (`attach_tma`
   compared field by field with the old function's output; core ids are
   random and operations carry times).
7. Save without a map sheet → the document keeps the cores; snapshot "roi"
   with empty `cores` and the found `display_cores`; no exclusion linked;
   Step1/2's hand-over unchanged. Choosing the map sheet after that Save →
   the snapshot refuses with the new text; after numbering, Run and Save it
   is a TMA snapshot.
8. Existing Regions / TMA / dearray tests: no new failures against a HEAD
   run on the Mac.

Acceptance (user): the demo TMA (backlog 10) without a map sheet: Find
cores (no question asked), see and edit the cores, Save; then the demo map
sheet, numbering.

## 4. Decisions

* ~~Q1 names without a map sheet~~ — none (user). Stage 2 shows no names;
  inside the result files each core is still told apart by the id the
  document already gives it (`core_…`), which the user never has to read.
* ~~Q2 with a map sheet~~ — unchanged (user): numbered, ticked cores only.
* Q3 (stage 2, for its plan): without a map sheet all found cores run by
  default, each with a Run tick to leave one out (the same tick as with a
  map sheet).
* My view on the estimate (asked by the user): agreed — fully automatic,
  rough watershed segmentation, median. One change, §3.1.3: the median is
  weighted by area, so the fragments count without outvoting the cores.

## 5. Risks and rollback

* The estimate can be off on unusual slides (one or two cores only, mostly
  fragments, a large section that is not a TMA). The line always names the
  value used; wrong outlines are corrected with Regions' existing tools
  (merge, delete, draw). With no field there is no manual override: if
  this proves needed on real slides, it is a later change.
* Stage 1 changes nothing downstream. Stages 2-3 change what a slide
  without a map sheet analyses once it has found cores: older results stay
  as they are; a new run follows the new rule (stated in their plan).
* Rollback per stage: revert its commit. A Save with map-less cores still
  opens with stage 1 reverted; Find cores is then greyed out again until a
  map sheet is chosen.

## 6. Plan review (codex astra low)

v0.1-v0.3 (stage 1's map-sheet part): round 1 FAIL, two P2s (after Undo /
Revert of the attach, numbering reached a document without positions; the
downstream promise ignored that a study alone makes a TMA project) and
two P3s, all taken; round 2 FAIL, one P2 (the single-core `Number` bypassed
the gate), taken; round 3 PASS. Raw output: scratchpad
`codex_g11_plan_r1.txt` … `r3.txt`.

v1.1 (the widened scope, the automatic estimate, the stages): to be
reviewed by codex.

v1.1 round 1 FAIL (scratchpad `codex_g11_plan_v11_r1.txt`), all taken in
v1.2:
* P2: dropping specks below 0.05 % of the image would drop every core on a
  large sparse slide (a 2000 px core on 100 000² px is 0.03 %). §3.1.2:
  an absolute 20 px at the sampling resolution; test the sparse canvas.
* P3: the sampling level had no rule for small images or slides without a
  pyramid. §3.1.1: level 0 when no level reaches 1024; block-mean
  reduction to ≤ 2048 px; tests.
* P3: the watershed seeds and the median's ties were not defined. §3.1.3-4:
  h-maxima per piece with h = 0.3 × its largest distance, plateaus one
  marker, ties to the smaller; tests with stated area shares and unequal
  touching cores.
* P3: existing tests require a missing diameter to fail. The keywords stay
  for scripts and tests; the one test is rewritten (§3.3).

v1.2 round 2 PASS (scratchpad `codex_g11_plan_v12_r2.txt`), no findings.

## 7. Stage 1 implementation (uncommitted, 2026-11-11)

User (TG): "可以实施阶段1".

* `core/tma/dearray.py`: `ESTIMATE` (the §3.1 numbers), `CoreSizeError`,
  `estimate_level`, `block_factor`, `_weighted_median`,
  `estimate_diameter_px`, `estimate_core_diameter`; `detect_cores`
  estimates when no size is given (`core_diameter_source` "estimated" /
  "given").
* `core/regions/document.py`: `attach_candidates`, `attach_map`;
  `attach_tma` = `_build_pieces` + `_set_positions` (the old body, split).
* `ui/regions/cores_panel.py`: `nucleus_channel`, `can_attach_map`,
  `map_blocker`; `sync_enabled` (Find cores without a study; the map button
  reads `Attach map sheet` when it attaches; the numbering panel follows
  `numbering_allowed`); `numbering_allowed` needs the document's map sheet;
  `select_preview_pending` uses it; `refresh` lists cores found without a
  map sheet.
* `ui/regions/cores_actions.py`: `find_cores` (no diameter question),
  `_detect_done` (`attach_candidates` without a study; the size in the
  line), `size_text`, `choose_map` (choose, then attach to found cores; or
  attach the existing study), `bind_single` and `confirm_numbering` refuse
  without the document's map sheet.
* `ui/regions/page.py`: `annotate_blocker` texts.
* `ui/tma/workers.py`: a `CoreSizeError` is reported as its own sentence.
* `core/regions/lock.py:214`, `core/regions/analysis.py:198`: text only.
* Tests: new `tests/test_tma_core_size_estimate.py` (14) and
  `tests/test_regions_find_cores_without_map.py` (6);
  `tests/test_tma_f2_dearray.py`: the missing-diameter test rewritten
  (§3.3). **Beyond §3.3's list**: three assertions on the old message text
  in `tests/test_tma_f3_page.py:441-442` and `tests/test_f4_analysis.py:227`
  now look for the new text (§3.2.8); nothing else in those files changed.

Found while testing (not changed, today's behaviour): a deeply
overlapping pair of unequal cores (1 : 0.7, centres 0.9 of the radii
apart) stays one piece, with the estimated size and with the true size
alike (solidity 0.92 is above `split_solidity` 0.85). The test checks that
the estimate splits exactly as the true size does; the user merges / draws
such cases by hand, as today.

Real demo TMA (backlog 10, `exemplar002_tma_4cores.ome.tif`), no size
given: estimated 2284 px = 1.48 mm, 4 cores found in 0.8 s (measured radii
1109-1271 px).

Regression (one process per test; 17 Regions / TMA / analysis files, the
change against a HEAD worktree): 261 tests on the change, 241 on HEAD.
The 4 HEAD failures fail the same on the change (`test_f4_step2 …[run]`,
`test_f4_step4::test_a_real_step2_run_with_regions…`,
`test_f5_regions_panels::test_the_channels_frame_is_step1s`,
`test_regions_r3::test_an_old_left_out_piece…` timeout). Two failed on the
change only (`test_regions_annotate_blocker`: its stand-in page has no
`holder`); `annotate_blocker` now reads it with `getattr`; both pass.

Code review round 1 (codex astra low, scratchpad `codex_g11_code_r1.txt`):
FAIL.
* P2: an Annotate tool (Line / Clicks) kept active across an Undo of the
  attach still takes numbering gestures and shows a preview that takes
  Ctrl+Z (Confirm is refused). The fix is one condition in
  `ui/regions/tools.py` `can_number` (the document's map sheet), outside
  §3.3's list: **asked the user**.
* P3, taken: the map button's words now follow the state (`map_unattached`)
  and its enabled state where it can be done (`can_attach_map`), so a
  read-only version or an ROI workspace shows a disabled `Attach map sheet`.
* P3, not taken: "an unequal touching pair must be found as two". HEAD's
  detection with the true size does not split a 1 : 0.7 pair even when the
  discs only touch (checked); that is the splitting rule, not the estimate,
  and not in stage 1. The test keeps "the estimate splits as the true size
  does".

User (TG): "同意改tools.py". `ui/regions/tools.py` `can_number` now also
needs the document's map sheet (its three callers: Line press, a finished
line's clicks, Clicks). Test: a Line tool left active across the Undo of
the attach takes no line and no click, and leaves no preview
(`test_numbering_follows_the_documents_map_sheet`); without the new
condition that test fails (checked).

Code review round 2 (scratchpad `codex_g11_code_r2.txt`): PASS, no
findings.

Regression after round 2 (the same 17 files, one process per test): 261
tests, 4 fail, exactly HEAD's 4; none new.

Acceptance: the user (TG, 2026-11-11) accepted stage 1 on the Mac with the demo TMA: "第11项真人验收通过".
