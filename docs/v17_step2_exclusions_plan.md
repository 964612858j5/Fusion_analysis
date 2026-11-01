# v17 Step2 follows the Regions exclusions on a slide without TMA cores — plan v2.1 (2026-11-01)

v2.1: round 2 — no P1/P2; its two P3 applied (§3 interrupted-Save test,
§2 method wording).
v2: codex plan review round 1 (3 P2 + 1 P3, no P1; diagnosis and fix
confirmed) answered — see §6.

Source: user real-machine report (TG 2026-11-01). TMA2_test, a TMA slide run
as tissue (no Find cores, no map sheet); a blur and a fold exclusion drawn
in Regions ("Step 0.5") and saved; Step2 run; Step3 shows cells inside the
blur exclusion as if it did not exist.

## 0. What happened (read-only check of /sdb1/Fusion/TMA2_test)

* `tma/regions.json`: Save 1 at 06:42:57 and Save 2 at 07:20:20, both with
  2 exclusions (`excl_2382f7885a0c` blur, `excl_edee52c698da` fold), both
  `channels: "all"`, 0 cores, no map sheet. There is no `tma/tma_study.json`.
  (The first Save did contain both — the user's impression that it had not
  been saved is not what the files say.)
* Step1 pre-segmentation `preseg_20261101_064547` DID follow them: its
  `regions_nucleus.json` is an `"roi"`-mode snapshot with both exclusions,
  `seg_invalid: true`.
* Both Step2 runs (`segment_..._064928` from the Step1 hand-over,
  `segment_..._072403` with manual parameters) have NO
  `regions_snapshot.json`, no `global_cell_regions*.npz`, and provenance
  without `regions`: they segmented the whole slide, exclusions ignored.

## 1. Cause

Since the user's ruling of 2026-10-19 (`docs/f5_tma_display_plan.md` §11),
Step2 drops the Step1 hand-over's frozen Regions
(`Step2Page._without_handover_regions`) and always freezes them at Run
from the last Save through `MainWindow._step2_manual_regions`
(`ui/main_window.py:2235`). That function was written for TMA cores (§9.2)
and returns `(None, "")` — "the old run, whole region" — whenever the
project has no TMA study for this slide (line 2262):

```
if study is None or study.get("slide_id") not in (None, sid):
    return None, ""                      # not a TMA project on this slide: the old run
```

So on a slide without TMA cores the saved exclusions are never frozen for
Step2, although `analysis.build_snapshot` builds an `"roi"`-mode snapshot
(exclusions only, the whole region is the domain) for exactly this case,
Step1 uses it (`_preseg_regions`), and the worker runs it
(`AnalysisRaster.tma == False`: every tile is in the domain, objects in a
segmentation-invalid exclusion are dropped with reason `exclusion`,
measurement-only ones get their bits). §11 removed the only path that
carried them (the hand-over's) without adding this case to the freeze.

## 2. Fix

`_step2_manual_regions`, the branch above only. In this order:

1. no TMA study for this slide (none, or another slide's) → read the Save
   for this slide (`RK.load_saved(pd, sid)`):
   * no Save → `(None, "")` as today (the old run);
   * a Save that cannot be read → the refusal the TMA branch already gives
     (`"the Regions result cannot be read (...): Save again in Regions"`);
   * a Save WITH a map sheet (`doc.get("map_sha256")`) → `(None, "")`,
     unchanged (not part of this report; a separate decision);
   * a Save without a map sheet and with ZERO exclusions in its document
     (`not doc["exclusions"]`) → `(None, "")`: the existing execution and
     output path, unchanged (codex §5 answer);
2. otherwise (no map sheet, at least one exclusion) → the same checks the
   TMA branch makes (method has an input kind; the fused input's region and
   fusion settings), then
   `RA.build_snapshot(pd, sid, seg, study=None, parent=parent, saved=saved)`
   (mode `"roi"`); an interrupted Save (`table_matches`) or any
   `RegionsError` / `AnalysisError` → its text as the refusal. Same request
   shape (`snapshot`, `identity`, `input_kind`, `selection_mode`,
   `fusion_settings_hash`). No tick question (there are no cores). One log
   line: `[Step2] Regions exclusions frozen for this run: N exclusions (M stop segmentation)`.

Nothing else changes: the worker, the snapshot format, Step1, Step3/Step4
readers, session restore, TMA projects (codex verified each). The domain is
the run's parent region (its polygon for a polygon ROI). An object touching
a segmentation-invalid exclusion by ANY pixel is dropped whole (the
existing rule, `core/regions/objects.py:104`); objects in a measurement-only
exclusion stay and carry its bit (Step4 sets those channels to NaN). A run
with exclusions writes `regions_snapshot.json` and
`global_cell_regions*.npz` and records `regions` in provenance.

Known consequence to state to the user: Step2 runs already made on such
slides (both TMA2_test runs) ignored the exclusions; they must be run again
(nothing is changed in existing results).

Refusal wording (codex P3): the Step2 page wraps every refusal in "This run
cannot follow the TMA cores." (`ui/step2_page.py:3459`), misleading on a
slide without cores. Proposed: that warning's title "TMA cores" → "Regions"
and its sentence → "This run cannot follow the Regions result: ..." — one
call; on the new branch the unsupported-method
reason says "{method} cannot follow the Regions exclusions (it has no
per-cell rules): choose another method" instead of "...the TMA cores..." — added to the whitelist for the
user to authorize with the rest (§4).

## 3. Tests

`tests/test_f5_manual_regions.py` (new no-map fixture: a Regions document
without a map sheet, saved with `K.save`, no study):
* the existing `freeze3` assertion is KEPT (map-bearing Save, no study →
  `(None, "")`);
* no-map Save with a blur exclusion (all channels) and a measurement-only
  exclusion (a non-segmentation channel) → request with an `"roi"`
  snapshot, both exclusions, `seg_invalid` True / False, no question asked;
* no-map Save with zero exclusions → `(None, "")`; no Save, no study →
  `(None, "")`;
* refusals on the new path: unreadable Save (corrupt `regions.json`),
  a method without an input kind, a fused input without fusion settings,
  and an `AnalysisError` raised by `build_snapshot` (patched in the test:
  refusal propagation only — a no-map Save has `table=None`, so the
  existing table-mismatch check cannot trip on it, and no hardening is
  added);
* worker level, both loops (the harness of
  `test_a_manual_run_follows_the_snapshot_step2_froze`), an `"roi"`
  snapshot: an object mostly OUTSIDE a seg-invalid exclusion but touching it
  is gone entirely; an untouched object stays; `dropped["exclusion"] > 0`;
  a surviving object inside a measurement-only exclusion has that bit in
  the written `global_cell_regions*.npz`.
* Mutation: with the old early return the new freeze tests fail.
* Regression (targeted): f5 manual regions, f4 step2 / analysis / step4,
  regions r1–r3, preseg contract, step2 legacy stop, step2 prefetch settles.
* Real-machine acceptance (user): on a slide without cores, draw a blur
  exclusion in Regions, Save, run Step2, open the result in Step3: no cell
  touches the blur area; elsewhere as before.

## 4. Scope (closed whitelist, AGENTS.md rule 1)

* `ui/main_window.py` (`_step2_manual_regions` only)
* `ui/step2_page.py` (the one refusal sentence, line 3459) — if authorized
* `tests/test_f5_manual_regions.py`
* this plan

## 5. Open question for review (answered in v2: zero exclusions keep the old run)

A non-TMA Save with zero exclusions: freeze an empty `"roi"` snapshot (the
run then writes the Regions files, all cells `analysis_include`) or keep the
old whole run with no Regions files? Proposal: freeze only when the Save
has at least one exclusion, so runs on slides that never used exclusions
stay byte-identical to today.

## 6. Codex plan review round 1 — answers

* P2 worker test weaker than the rule → tests an object mostly outside but
  touching (dropped whole) and measurement bits on disk, both loops.
* P2 keep `freeze3` → kept; a separate no-map fixture.
* P2 refusal branch untested → unreadable / interrupted Save, no input
  kind, no fusion settings on the new path; real-machine acceptance added.
* P3 refusal wording → one sentence, proposed for authorization (§2, §4).
* §5 → zero exclusions keep the old run, checked from the document before
  the method / fusion checks.

## 7. Implementation notes (authorized 2026-11-01)

* `_step2_manual_regions`: `tma` = a study for this slide. Not TMA and no
  slide id → the old run (as before). Otherwise the Save is read first
  (unreadable → refusal); not TMA and (no Save, a map sheet, or no
  exclusion) → the old run; not TMA otherwise → method / fused-input checks
  (method wording "cannot follow the Regions exclusions (it has no per-cell
  rules)"), `build_snapshot(study=None)`, any `RegionsError` its refusal,
  the log line, the same request. The TMA branch is unchanged.
* One consequence of reading the Save first: a project without a study for
  this slide whose Save is for ANOTHER slide (`load_saved` raises) is now
  refused with "the Regions result cannot be read (... another slide ...)"
  instead of running whole — the same refusal Step1's pre-segmentation
  already gives there.
* `ui/step2_page.py`: the warning is titled "Regions", "This run cannot
  follow the Regions result: ...".
* Tests (`test_f5_manual_regions.py`, +7): freeze with a blur (all
  channels) + a measurement-only exclusion → `"roi"` request, no question;
  no Save / no exclusion / map sheet without study → old run; refusals
  (method, fusion settings, patched `AnalysisError`, unreadable Save);
  worker, both loops: every object touching a blur box is gone whole (a
  mostly-outside one exists in the data), the rest identical to the plain
  run; a measurement-only box drops nothing and sets bit 1 exactly on the
  cells touching it. Mutation: old function → the 2 freeze tests fail (the
  worker tests pass on the old code: the worker is unchanged).
* Read-only check on TMA2_test's Save: `build_snapshot(study=None)` gives
  `"roi"`, 0 cores, both exclusions `seg_invalid` (nothing written there).
* Regression: f4 step2/analysis/step4 59, regions r1–r3 84, preseg contract
  + legacy stop + prefetch settles 42, step2 layout + restart params + f5
  manual regions + Step1→Step2 hand-over 51 — all pass.
* Codex code review round 1: no P1/P2; its P3 (the drop count) applied —
  the blur test reads the run's Regions drop record from
  `segmentation_meta*.json`: `dropped["exclusion"]` equals the number of
  objects touching the box.
* Codex code review round 2: PASS. Real-machine acceptance by the user
  (2026-11-01): passed.
