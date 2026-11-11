# Cellpose whole-cell + nucleus — plan v1.4, 2026-11-10 (v1.4: §15)

Requested by the user (TG 2026-11-10, backlog #2): a new segmentation method.
Cellpose segments the whole cells AND the nuclei from Step1's image; the two
are then paired: a nucleus that is not inside one cell (two cells, a cell's
border, background) is dropped; a cell may keep several nuclei; **a cell with
no nucleus is dropped** (user ruling, TG 2026-11-10). Plan only: codex review,
then the user's approval, then implementation.

## 1. Today (read-only, HEAD b2c5705)

* Registry: `utils/segmentation_config.py:11-21, 23` (`SEGMENTATION_METHODS`);
  Step1's list `utils/segmentation_param_schema.py:29-33` (`UI_METHODS`) and
  `METHOD_PARAMS` (113-122), Cellpose specs `_cellpose` (70-81: diameter, flow,
  cellprob listable; min size).
* Engine (`seg_runner/engines.py`): `METHOD_OUTPUTS` (27-36),
  `METHOD_ENGINE` = name prefix (37); `CellposeEngine.predict(image, params)`
  (≈219-247, one cpsam model per process); `run()` (≈417-448) returns
  `{"cell", "nucleus"}` — Mesmer nuclear-guided one pass with two
  compartments, the expansions `predict` then `expand`, every other method one
  `predict`. The runner already carries two masks back
  (`seg_runner/runner.py:110-135`, `<task>.cell.npy` / `<task>.nucleus.npy`); no
  protocol change. Identity is per engine (`BEHAVIOR_VERSION["cellpose"] = 1`).
* Input (`core/preseg_input.py:23-32, 123-135`): `cellpose_wholecell_fusion` →
  `fusion_fusion_nucleus` = `[f0, f0, f1]`; its channel 2 is exactly the
  `nucleus` input (`f1`) of the nucleus methods. One task = one input array.
* Pairing exists: `core/nuclei_pairing.py:36 pair_nuclei(cells, nuclei)` keeps a
  nucleus only when ALL its pixels lie in ONE cell (drops `multiple_cells`,
  `partial_background`, `outside_cells`), several nuclei per cell allowed,
  kept nuclei renumbered 1..K with `nucleus_cell[k]`. Step2 runs it per tile for
  `SECONDARY_NUCLEI_METHODS` (`workers/segment_merge_worker.py:685`,
  `_segment_tile_contract` 2645-2693). **No step drops cells without a
  nucleus** today.
* Step1 does not pair: `ui/step1_presegmentation/run_job.py:310-332 own_masks`
  owns each output separately (`paired=False`) except the expansions (nucleus
  id = cell id).
* Step2 merge: `core/seam_merge.py SeamMerger._write` (≈374-413) writes a
  cell, then each of its nuclei only if all its pixels are in the cell's
  written pixels (else `nuclei_dropped_seam_conflict`); so after pairing a
  cell can still lose all its nuclei at a seam (also: oversized and Regions
  drops take the cell with its nuclei — no orphan there).
* Step3 (`core/step3_masks.py:41-53`): a method in none of its sets is
  "unknown: not shown" (`resolve_masks` 588-643). Step4 maps nuclei through
  `nucleus_to_cell` (`core/quant_engine.py:17-23`), several nuclei per cell
  supported (`n_nuclei`, cytoplasm = cell minus its nuclei); no change needed.

## 2. Proposed behaviour

* New method `cellpose_wholecell_nuclei`, shown as **"Cellpose whole-cell +
  nuclei (Fusion + DAPI)"**, listed after `cellpose_wholecell_fusion`; outputs
  `("cell", "nucleus")`; engine `cellpose` (name prefix).
* Input: `fusion_fusion_nucleus` (one task, one array, as whole-cell). The
  engine runs the ONE Cellpose model twice: cells on `[f0, f0, f1]` (exactly
  `cellpose_wholecell_fusion`), nuclei on channel 2 = `f1` (exactly
  `cellpose_nuclei_dapi`'s input). No new input kind, no protocol change.
* Parameters: the cell pass takes the whole-cell set (`diameter`,
  `flow_threshold`, `cellprob_threshold`, `min_size`; the first three listable
  in Step1 as today); the nucleus pass its own set `nucleus_diameter`,
  `nucleus_flow_threshold`, `nucleus_cellprob_threshold`, `nucleus_min_size`
  (defaults = `cellpose_nuclei_dapi`'s; **not listable** in v1, so a Step1
  grid does not multiply — Q2). The engine returns the RAW cell and nucleus
  masks (like Mesmer nuclear-guided).
* Pairing + the new rule, ONE pure function both steps call:
  `core/nuclei_pairing.py keep_cells_with_nuclei(cells, nuclei)` =
  `pair_nuclei`, then cells with no kept nucleus set to 0 (counted
  `cells_without_nucleus`); kept nuclei keep `nucleus_cell`. Cell ids are not
  renumbered here (ownership / the seam merge number them).
  * Step1 (`run_job.py own_masks` / `_finish_ok`): for this method the pair is
    made on the WINDOW (the patch plus its halo, what the engine saw), then
    the cells are owned by centroid as today and a kept nucleus follows its
    PARENT cell through `nucleus_cell` (kept iff its cell is kept; separate
    nucleus ids — not `apply_ownership(shared=)`, which assumes equal ids).
    The rule is decided on the window; the preview is then cropped to the
    patch as today, so at the patch edge a kept cell can show without its
    nucleus (the nucleus lies outside the patch) — visibility, not the rule.
    The record counts cells (as today); the drops (nuclei by reason,
    `cells_without_nucleus`, owned by centroid) are published with the record
    (`core/preseg_run.py publish_result`, an optional `drops` field) and shown
    on the result's line in Results, also after a reload / reuse.
  * Step2: the method joins `SECONDARY_NUCLEI_METHODS`; `_segment_tile_contract`
    calls `keep_cells_with_nuclei` instead of `pair_nuclei` for it (so a tile's
    nucleus-less cells never become candidates); `SeamMerger` gets
    `require_nucleus` (on for this method only): `_write` checks which of the
    candidate's nuclei fit its free pixels BEFORE it takes an id; none fits →
    the cell is not written (`cells_dropped_no_nucleus_at_seam`). Every written
    cell then has ≥1 nucleus; ids stay 1..N / 1..M.
  * The label store's `nuclei` record adds `cells_without_nucleus` (owned
    tile counts, as `_nuclei_count_drops` does for nuclei) and the seam count.
* Step2 page: the method in its combo (from `available_segmentation_methods`);
  `_on_method_changed` shows the Cellpose rows plus four nucleus rows for it;
  the four nucleus parameters round-trip everywhere the Cellpose ones do —
  `_apply_method_defaults_to_ui`, `_apply_seg_config_to_ui` (a loaded file /
  Step1 hand-over / draft), `get_seg_config`, and
  `normalize_segmentation_config`'s flat-key list (`utils/segmentation_config.py`
  ≈394, ≈509), `None` kept for an auto diameter — so non-default Step1 values
  reach the engine unchanged and the contract check passes.
* Recovery (`.npy` recovery mode) has no nuclei and no pairing: Step2
  **refuses** a recovery run for this method before it starts, saying why
  (`workers/segment_merge_worker.py` ≈690, and the page's Run check). Tile strategy: Cellpose
  (`utils/tile_strategy.py:12-21`).
* Step3: `_PRIMARY_CELL_AND_NUCLEI` (cell primary, nuclei beside). Step4: no
  change. Regions: as the other two-output methods (`regions_filter`: the
  nucleus follows its cell).
* Identity: no `BEHAVIOR_VERSION` bump — the existing Cellpose methods do not
  change; the new method has no older results.

## 3. Closed whitelist

* `utils/segmentation_config.py` (entry), `utils/segmentation_param_schema.py`
  (`UI_METHODS`, `METHOD_PARAMS`, nucleus specs), `seg_runner/engines.py`
  (`METHOD_OUTPUTS`, the `run` branch: two `predict` calls), `core/preseg_input.py`
  (`INPUT_KIND`), `core/nuclei_pairing.py` (`keep_cells_with_nuclei`),
  `core/seam_merge.py` (`require_nucleus`), `ui/step1_presegmentation/run_job.py`
  (`own_masks` for this method), the Results line in
  `ui/step1_presegmentation/results_panel.py` only if the drop counts need a
  slot, `workers/segment_merge_worker.py` (`SECONDARY_NUCLEI_METHODS`, the
  tile call, the merger flag, the record), `ui/step2_page.py` (method rows),
  `utils/tile_strategy.py`, `core/step3_masks.py` (method set).
* Not changed: the protocol, the runner, `pair_nuclei`, Step4, the Viewer,
  Mesmer / StarDist, the legacy Phase1/Phase2 panel (off the screen; only made
  not to break on the new name).
* `core/preseg_run.py`: `publish_result` takes and stores an optional `drops`
  dict (nothing else).
* Tests: new `tests/test_cellpose_cell_nucleus.py`; the method-enumerating
  tests get the new method where they list all methods
  (`test_seg_runner_engines.py:37-43`, `test_preseg_run.py:104-118`,
  `test_step1_method_plan.py:36,106`, `test_preseg_contract.py:38-58`,
  `test_step2_runner_path.py:48-67`, `test_step3_masks.py:218-227`, …).
* Docs: both user guides (Appendix A method list, Step2 rows),
  `UI_SURFACE_RULES.md` if it lists methods.

## 4. Tests and acceptance

* `keep_cells_with_nuclei`: inside / two cells / border / outside; two nuclei
  in one cell kept; a cell whose only nucleus is dropped is dropped; ids.
* `SeamMerger(require_nucleus=True)`: interior, seam and recovered
  candidates; one whose nuclei all conflict is not written; oversized and
  Regions rejections take the pair; several nuclei per cell; ids consecutive;
  `validate` holds AND, separately, every written cell id appears in the
  nucleus table (validate checks containment only); off = today.
* Engine (real Cellpose, CPU, synthetic image): two masks; the cell mask equals
  `cellpose_wholecell_fusion`'s and the nucleus mask `cellpose_nuclei_dapi`'s
  on the same input and params (wiring check).
* Step1: a run of the method on a synthetic project — on the window result
  (before the crop) every owned cell has a kept nucleus and every kept
  nucleus lies in one owned cell; a patch-edge case where the cell is kept
  and its nucleus lies outside the patch; the drop counts after a reload.
* Parameters: non-default nucleus values (and an auto diameter) chosen in
  Step1 → Use → Generate → Step2 reach the engine unchanged; the contract
  check passes; defaults reset with the method.
* Recovery mode with this method is refused before anything runs.
* Step2 (fake engine as `test_step2_keeps_nuclei.py`, plus one real-engine
  case): the final store has no cell without a nucleus; `nucleus_to_cell`
  consistent; Step4 `n_nuclei ≥ 1` for every cell.
* Real machine (Mac, `cropped_region.ome.tif`): Step1 pre-segmentation with
  the method → Use → Generate → Step2 → Step3 shows cells and nuclei → Step4.

## 5. Risks

* The "all pixels inside one cell" rule is strict: Cellpose cells and nuclei
  are predicted independently, so a nucleus sticking out of its cell by one
  pixel drops it, and with it a cell that had only that nucleus. The drop
  counts are shown in Step1 and recorded in Step2 so the user can judge it on
  real data (Q3).
* Two Cellpose passes per tile: about twice the whole-cell time.

## 6. Questions for the user

1. Nuclei from the nucleus (DAPI) channel of Step1's image, cells from fusion
   + nucleus (as the two existing methods) — the backlog wrote "both from the
   fusion channels"; this plan reads it as both from Step1's one image.
2. Nucleus parameters: separate values, not listable in v1 (recommended), or
   listable too (Step1 grids grow by their product)?
3. The pairing rule stays strict ("every nucleus pixel in one cell", as
   Step2 does today) — or should a small tolerance be allowed (e.g. ≥ 95 % of
   the nucleus in one cell, the rest trimmed)? Recommended: strict in v1,
   decide after seeing the drop counts on real data.

## 7. Codex plan review round 1 (astra low) — FAIL (P1 ×1, P2 ×3), taken in v1.1

* P1: `.npy` recovery has no nuclei → §2 recovery refused for this method, §4
  test.
* P2: Step1 patch-edge semantics → §2 (rule decided on the window, crop is
  visibility only; nuclei follow the parent cell, not `apply_ownership`), §4.
* P2: nucleus parameters must round-trip (load / reset / get / normalize,
  `None` for auto) → §2, §4.
* P2: Step1 drop counts need a persisted field → `publish_result` `drops`,
  whitelisted (§3), checked after reload.
* §4 asserts nucleus coverage of every cell separately from `validate()`.

## 8. Codex plan review round 2 (astra low) — PASS

All round-1 findings resolved in v1.1; no new public-path gap; hardening stops (AGENTS rule 7). Open: the user's answers to §6 Q1–Q3 and approval to implement.

## 9. User rulings (TG 2026-11-10) and approval

1. Q1 agreed: both from Step1's one image (cells fusion + nucleus, nuclei the nucleus channel).
2. Q2: nucleus parameters are not grid-searched (single values).
3. Q3 agreed: strict rule in v1.

Approved: "可以开始写代码了".

## 10. Implementation (2026-11-10, on b2c5705)

* `utils/segmentation_config.py`: `CELLPOSE_WHOLECELL_NUCLEI =
  "cellpose_wholecell_nuclei"` after whole-cell; its four `nucleus_*`
  defaults; the four keys in `normalize_segmentation_config`'s flat list.
* `utils/segmentation_param_schema.py`: in `UI_METHODS` (9 methods);
  `_cellpose_with_nuclei()` = the whole-cell specs + four nucleus specs, not
  listable (ruling Q2); "0 = auto" also for `nucleus_diameter`.
* `seg_runner/engines.py`: `METHOD_OUTPUTS` ("cell", "nucleus");
  `CELL_AND_NUCLEUS_METHODS`; `nucleus_pass_params` (defaults kept in the
  engine module, which imports nothing of the application); `run`: two
  `predict` calls on one model, the nucleus pass on the input's last channel;
  raw masks.
* `core/preseg_input.py`: `INPUT_KIND` = `fusion_fusion_nucleus`.
* `core/nuclei_pairing.py`: `keep_cells_with_nuclei` (pair, then the cells
  without a kept nucleus set to 0, ids kept, `cells_without_nucleus`).
* `core/seam_merge.py`: `SeamMerger(require_nucleus=)`: before a cell takes
  an id, at least one of its nuclei must be writable, else
  `cells_dropped_no_writable_nucleus` (+ its nuclei as seam conflicts).
* `workers/segment_merge_worker.py`: the method in
  `SECONDARY_NUCLEI_METHODS` and `CELLS_NEED_NUCLEUS`; the tile calls
  `keep_cells_with_nuclei`; the dropped cells counted by centroid through
  `_nuclei_count_drops`; the merger flag; `label_store.nuclei.cells_dropped =
  {without_nucleus, no_writable_nucleus}`; Recovery Mode refused at start.
* `ui/step1_presegmentation/run_job.py`: `own_cell_nucleus_masks` (pair on
  the window, cells owned by centroid, nuclei follow the parent cell, crop;
  drops by centroid); `core/preseg_run.py publish_result(drops=)`.
* `ui/main_window.py` — outside §3's list, a few lines: the Results line adds
  "N cells without a nucleus and M nuclei dropped" from the records (§2 asked
  for it; the line is built there, not in `results_panel.py`).
* `ui/step2_page.py`: four nucleus rows (this method only), round-trip in
  `_apply_seg_config_to_ui` / `_apply_method_defaults_to_ui` (`_set_nucleus_pass`)
  / `get_seg_config` (only this method carries the keys); Recovery Mode
  refused before the run.
* `utils/tile_strategy.py`, `core/step3_masks.py`: the method registered.
* Tests: new `tests/test_cellpose_cell_nucleus.py` (10); method tables
  extended: `test_seg_runner_engines.py`, `test_preseg_run.py`,
  `test_step1_method_plan.py` (9 methods), `test_preseg_contract.py` COMBOS
  (non-default nucleus values, auto nucleus diameter — so the Use → Save →
  Step2 e2e covers the hand-over round trip).
* Not in the Step2 runner-path tables (`test_step2_runner_path.py` PARAMS):
  that module's run tests fail on HEAD already (the worker's finish signal),
  and its real-engine runs are heavy; the new method's Step2 run is covered by
  the stand-in-engine test (raw OME pyramid given, the meta file checked).
* Mac: the new tests pass (10/10, the engine one on the Mac's GPU — on the
  CPU four Cellpose passes exceeded 600 s). A first regression was stopped by
  Claude Code for low memory (17 orphaned engine processes from crashed test
  processes were found and stopped); rerun with 2 parallel tests.

## 11. Codex implementation review round 1 (astra low) — FAIL (P2 ×1), fixed

* P2: a file / draft of this method loaded, then another method chosen by hand in Step2: its `nucleus_*` keys went with the other method. Fix: `get_seg_config` drops the four keys for every other method; the page test now loads the file into `_seg_config` and switches the method.
* Advisory taken: the engine test writes the nucleus pass's expected values out instead of using `nucleus_pass_params`.
* Mac regression (2 tests at a time): 20 modules + the new file, same 35 failures as HEAD, none new, 12 more passing; after the fix the touched modules again show no new failure.

Round 2 (astra low): **PASS** — the fix holds, no new public-path defect; hardening stops (AGENTS rule 7). Waits for the user's real-machine acceptance.

## 12. v1.2 — the pairing rule relaxed (user, TG 2026-11-10, after real-machine look)

### 12.1 Why

On the user's 4 Step1 patches (`cropped_region.ome.tif`, test project, default
parameters) the strict rule kept 165 of 1105 cells (15 %): Cellpose's
whole-cell masks are about nucleus-sized (median cell area below the median
nucleus area in 3 of 4 patches), so independent nuclei stick out or straddle
two cells; only 8 cells touch no nucleus pixel at all. Checked how Mesmer's
ecosystem does it: DeepCell's `mesmer_postprocess(compartment="both")` pairs
nothing; ark-analysis takes, per cell, the nuclear label with most pixels in
the cell (one nucleus per cell, optional cell ∩ nucleus split, cells without a
nucleus kept); SPACEc only subtracts the masks. The user chose majority
assignment, then — because the fusion's channel choice makes the whole-cell
mask less reliable than the DAPI nucleus — UNION instead of trimming, plus
nucleus-born cells.

### 12.2 Rules (user rulings; the 50 % and 2 px fixed, not parameters)

Per window (Step1's patch + halo, Step2's read window), in this order:

1. **A cell wholly inside one nucleus** (every pixel of the cell is that
   nucleus) is discarded: the nucleus becomes a cell of its own (rule 3).
2. Each nucleus: the share of its pixels in each remaining cell and in
   background.
   * **≥ 50 % in one cell** → it belongs to that cell, and the cell becomes
     cell ∪ nucleus (the nucleus's pixels that lay in background or in another
     cell join it: the DAPI nucleus is trusted over the fusion's cell border).
3. * **≥ 50 % in background** (no cell for it — e.g. its marker is not in the
     fusion) → a new cell = the nucleus (its pixels taken from wherever they
     were) grown by **2 px into background only** (it stops at other cells;
     between two new cells the nearer wins, `expand_labels`).
4. * neither (split over two or more cells, none ≥ 50 %, background < 50 %)
     → the nucleus is dropped (`nuclei_ambiguous`).
5. A cell left with no nucleus is dropped (ruling 2026-11-10 unchanged); a
   cell that lost all its pixels to other nuclei is gone.

Several nuclei per cell stay allowed; every kept nucleus lies wholly in its
cell by construction; nuclei never overlap each other (one label image), so
two unions never compete for a pixel.

Measured on the 4 patches (owned by centroid): 1211 cells kept (was 1105
Cellpose cells): 870 Cellpose cells (grown by their nuclei) + 341 cells born
from a nucleus; 231 Cellpose cells gone (wholly inside a nucleus, or no
nucleus); 12 ambiguous nuclei dropped (whole windows); 0 kept nuclei outside
their cell (`scratchpad cn_union.py` prototype).

### 12.3 Changes against v1.1

* `core/nuclei_pairing.py`: `keep_cells_with_nuclei` becomes
  `combine_cells_and_nuclei(cells, nuclei)` with the rules above → `(cells,
  nuclei, nucleus_cell, counts)`; counts: `cells_replaced_by_nucleus`,
  `nuclei_joined`, `cells_from_nucleus`, `nuclei_ambiguous`,
  `cells_without_nucleus`. Cells are relabelled 1..N (new cells included),
  nuclei keep one id each, `nucleus_cell` as before. `pair_nuclei` and every
  other method unchanged.
* Step1 (`own_cell_nucleus_masks`) and Step2 (`_segment_tile_contract`) call it
  instead; the rest of v1.1 stands: ownership by centroid with nuclei
  following their parent, `SeamMerger(require_nucleus)`, Recovery refused,
  drops published / recorded (new keys), Results line.
* Tests: the pairing tests rewritten for the five rules (cell inside a
  nucleus; union taking background and a neighbour's pixels; nucleus-born cell
  growing 2 px and stopping at a cell; ambiguous dropped; no-nucleus cell
  dropped; ids; every nucleus inside its cell); Step1 / Step2 tests adjusted.

### 12.4 Points for the review

* The union takes pixels from a neighbouring cell (the minority part of the
  nucleus) — that cell shrinks and may split into pieces; it keeps its id.
* Window edges: a nucleus cut by the read window is judged on what the window
  sees; ownership and the seam merge decide between windows as before.
* The 2 px growth only into background, fixed; the 50 % fixed.

### 12.5 Codex v1.2 review (FAIL, P2 ×6) and the user's rulings (TG 2026-11-10)

* Rule 1 (user): a cell wholly inside one nucleus is only REMOVED; the
  nucleus then goes by the ordinary rules (majority → union with that cell;
  background-majority → a nucleus-born cell). The prototype already did this.
* 50/50 between two cells (user): ambiguous, the nucleus is dropped. A
  cell/background 50/50 goes to the cell (the cell rule comes first).
* Growth (user agreed): 1 px per step (3 × 3 neighbourhood), 2 steps, into
  background only — it never crosses a cell; a pixel two new cells reach in
  the same step stays background; all claims of a step from the previous
  step, applied together.
* After rule 5 (user agreed): no regrowth into the freed pixels; a cell may be
  nearly all nucleus (empty cytoplasm possible); a neighbour cut into pieces
  by a union stays one cell (one id, measured together).
* Codex P2s taken: Step1's Regions filter runs on the COMBINED cells (nuclei
  follow their final parent) before ownership / crop; counts are attributed
  per object by centroid (the ids / masks of removed, dropped and ambiguous
  objects kept for `owned_drop_counts`), never whole-window totals; the final
  relabelling updates cells, nuclei and `nucleus_cell` together. The
  identity: no version bump (v1.1 was never committed, so no product result
  of the strict rule exists; a bump would invalidate every Cellpose method's
  reuse) — the user's test-project runs of the strict rule from today are to
  be deleted and re-run.
* Empty cytoplasm in Step4 (user asked; codex discussed): keep today's
  Step4-wide convention — every statistic of an empty region is NaN ("no
  pixels measured", not "zero signal"), `cytoplasm_area` = 0. A change to 0
  (e.g. for `sum`) would be a separate, Step4-wide decision for every method,
  not part of this block.
* Rust or Python (user asked; codex discussed): Python (numpy / scipy,
  vectorised, no per-object loops). Measured on the Mac: 1.11 s for a
  4096² tile with ~15 000 cells and ~15 000 nuclei, under 1 % of the two
  Cellpose passes on such a tile; same counts as the prototype on the 4 real
  patches, every nucleus inside its parent. A Rust function would need new
  wheels for every platform for at most ~1 s per tile. Memory: 32-bit label
  buffers (64-bit only for the packed overlap keys), temporaries released
  before the growth; re-measure peak memory and time after.

Codex v1.2 round 2 (astra low): **PASS** — all six round-1 findings resolved; the candidate follows the rulings; per-object reporting is an integration requirement. Hardening stops. Waits for the user's answer on Q1 (NaN kept) / Q2 (Python) and approval.

## 13. v1.3 draft — a shared nucleus is split (user, TG 2026-11-10)

User: a nucleus over several cells may be shared out — in dense tissue DAPI
can merge two nuclei into one, so the cell a piece lies in keeps that piece;
the 50 % rule applies only to a nucleus that is NOT shared. Supersedes §12.5's
"50/50 between two cells: dropped" and the union taking a neighbour's pixels.

Draft rules, per window (1 and 5 as before):

1. A cell wholly inside one nucleus is removed (§12.5).
2. A nucleus on no cell → a nucleus-born cell (grown 2 px into background).
3. A nucleus on ONE cell (plus background): ≥ 50 % in it → union (its
   background part joins the cell); else → a nucleus-born cell (taking its
   pixels from that cell).
4. A nucleus on TWO OR MORE cells (shared) → split: each cell keeps its piece
   as a nucleus of its own (no pixel changes cell); the nucleus's background
   part goes to the cell with the largest piece (a tie: it stays background).
   A piece below 10 % of the nucleus is not a nucleus (ark-analysis drops
   split pieces under 5 px): it stays an ordinary pixel of its cell.
   Open: a shared nucleus mostly on background (e.g. 60 % background, 25 %
   in A, 15 % in B) — background part to A as above, or the background part
   becomes a nucleus-born cell?
5. A cell with no nucleus (piece) is dropped; no regrowth.

Measured on the 4 patches (owned cells): split with no minimum: 1132 cells
(241 nucleus-born) but 6033 nucleus pieces from ~3 800 nuclei — thousands of
1–2 px slivers along cell borders would count as nuclei; with the 10 %
minimum: 1121 cells (241 nucleus-born), 3891 pieces, 2142 slivers left as
cell pixels; 20 %: 1120 cells. (v1.2 union: 1211 cells, 341 nucleus-born.)

Step4 0 vs NaN: in Mesmer's ecosystem (ark-analysis `marker_quantification`)
a cell without a nucleus keeps its whole-cell row and its nuclear columns are
left at their initial zeros (no NaN); ark has no cytoplasm compartment.
DeepCell itself does not quantify.

### 13.1 Codex discussion of the v1.3 draft (astra low)

* Order made explicit: remove wholly-contained cells; classify every nucleus
  against THAT mask before any union or growth; "shared" = touching ≥ 2
  remaining cells BEFORE small pieces are filtered (filtering never turns a
  shared nucleus back into an unshared one); unshared: rules 2–3 (an exact
  50 % cell / background goes to the cell); shared: eligible pieces, then the
  background part; then the simultaneous 2-step growth; then rule 5.
* Minimum piece: ≥ 10 % of the nucleus's whole window-visible area (background
  included), measured BEFORE the background is allocated; exactly 10 % kept;
  no extra absolute floor (ark's 5 px not added); `min_size` /
  `nucleus_min_size` stay segmentation filters, not re-applied to pieces. A
  cell whose only contact is a rejected sliver is dropped; rejected slivers
  stay cell pixels (cytoplasm). One nucleus id per kept (source nucleus,
  parent cell) pair.
* Background part of a shared nucleus: to the unique largest ELIGIBLE piece,
  whatever its share (60 % bg / 25 % A / 15 % B → 85 % piece in A, 15 % in B);
  a tie for largest, or no eligible piece → it stays background. (The
  alternative — a nucleus-born cell from the background part — would make
  three objects of one DAPI object.)
* Consequences: `n_nuclei` counts kept pieces (not DAPI objects); counts kept
  separately (source nuclei split, pieces kept, slivers rejected); Step2: one
  id per piece, the SeamMerger accepts each piece on its own; Step1: combine →
  Regions → ownership → crop; Step4 unchanged.
* 0 vs NaN: ark-analysis's zeros do not change the recommendation — keep
  Step4's NaN for empty regions (area 0); a change would be its own Step4-wide
  block.
* Re-measured with these semantics (4 patches, owned): 1117 cells (241
  nucleus-born), 3795 nucleus pieces kept, 2369 slivers left as cell pixels.

### 13.2 User rulings on v1.3 (TG 2026-11-10)

* a. Minimum piece: agreed — a piece < 10 % of the nucleus (measured before
  the background is allocated) is not a nucleus; it stays a pixel of its cell.
* b. Background part of a SHARED nucleus: if the background is > 50 % of the
  nucleus it is DISCARDED (stays background; the pieces in cells stay their
  cells' nuclei); otherwise (≤ 50 %) it goes to the unique largest eligible
  piece (a tie or no eligible piece: discarded).
* 0 vs NaN: the user asked how MCMICRO handles it. MCQuant
  (labsyspharm/quantification, `mcquant/SingleCellDataExtraction.py`)
  quantifies each mask on its own with `regionprops_table` and writes one table
  per mask; a cell with no pixels in a mask (e.g. no cytoplasm) has NO row in
  that mask's table — no 0, no NaN; joined by CellID it becomes a missing
  value (NaN in pandas / NA in R). Recommendation unchanged: Step4 keeps NaN
  (= missing) for empty regions, area 0.

## 14. v1.3 implementation (2026-11-10; user: "保持 NaN，批准执行")

* `core/nuclei_pairing.py`: `keep_cells_with_nuclei` replaced by
  `combine_cells_and_nuclei` (rules §13 + §13.1 + §13.2; vectorised numpy /
  scipy, uint32 label buffers, uint64 packed keys only; constants
  `MAJORITY = 0.5`, `MIN_PIECE = 0.1`, `GROW_PX = 2`) returning cells 1..N,
  nucleus pieces 1..K, `nucleus_cell`, window counts and `objects` ({reason:
  (label image, ids)}); `owned_object_counts` counts them by centroid.
* Step1 (`run_job.py`): `combine_window` first, then the Regions filter on the
  combined masks, then `own_cell_nucleus_masks(raw, combined, own)` (cells by
  centroid, pieces follow their parent through `nucleus_cell`, crop); the
  record's `drops` = the owned events. Results line (`ui/main_window.py`):
  "N cells from a nucleus · M nuclei shared · K cells without a nucleus
  dropped".
* Step2 (`segment_merge_worker.py`): the tile result is the combined masks;
  `_nuclei_count_drops` counts tuple-valued events into `state["pairing"]`;
  the label store records `nuclei.pairing` (+ `cells_no_writable_nucleus`
  from the merge); `nuclei.kept` counts pieces.
* Tests: `tests/test_cellpose_cell_nucleus.py` rewritten for the rules (17
  tests: rule 1 + growth, union, minority → new cell, shared out, background
  > 50 % discarded, tie, sliver, growth stops at a cell and a contested pixel
  stays background, invariants on every case; Step1 window / edge / counts;
  Step2 store; seam merge; engine; page; recovery).
* Mac, real data (the 4 patches, owned): 1116 cells (prototype 1117), events
  202 cells inside a nucleus, 555 nuclei shared, 71 shared nuclei without an
  eligible piece, 25 cells without a nucleus, 241 cells from a nucleus. A
  4096² tile (~14 000 cells): 1.21 s, peak 436 MiB (tracemalloc).
* Found while measuring (for the user): a SHARED nucleus with no piece ≥ 10 %
  is lost whole (71 in the 4 patches, ~6 % of the cells' count) — e.g. a
  nucleus 85 % on background touching two cells with 8 % and 7 %; the same
  nucleus touching ONE cell would have become a cell of its own.

### 14.1 Codex v1.3 code review round 1 (astra low) — FAIL (P2 ×1), fixed

* P2: slivers (and discarded background parts) were in `counts` only, so the persisted records lost them. Fix: `objects` carries `nuclei_with_slivers` (counted per nucleus, not per sliver) and `nuclei_background_discarded`; both reach Step1 `drops` and Step2 `nuclei.pairing`.
* Advisories taken: exact owned-count test; the "shared nucleus without an eligible piece is lost whole" case pinned by a test (kept as approved, the user is told).

Round 2 (astra low): **PASS** — no new public-path defect; hardening stops (AGENTS rule 7).

## 15. v1.4 — straddling nuclei move their cell's border; a cell with two nuclei is split; StarDist on a near-blank tile (user, TG 2026-11-10)

### 15.1 Why

The user looked at `hybrid_cellpose_stardist` on the Mac and asked (discussed
with codex astra high and Fable 5.2 medium, the discussion in the session):

1. a nucleus that reaches into the neighbouring cell -- today a piece
   >= 10 % becomes a SECOND nucleus of that neighbour (wrong);
2. two nuclei of similar size in one Cellpose cell -- today one cell with two
   nuclei; the user wants two cells (a fragment stays with its cell).

Measured on the user's whole slide (hybrid, the user's Step2 parameters, raw
masks, 3072-px tiles + 200 px halo, objects owned by centroid; 46 535 cells,
56 694 nuclei, median nucleus 172 px; 3 near-blank edge tiles skipped, §15.4):

| piece of a nucleus in the non-dominant cell A | A has its own nucleus | A has none other |
|---|---|---|
| < 10 % | 14 251 | 834 |
| 10–30 % | 2 380 | 825 |
| >= 30 % | 266 | 647 |

Cells holding >= 2 nuclei each >= 50 % inside: 3 631 (7.8 %); smaller /
larger: < 0.25: 101, 0.25–0.5: 902, >= 0.5: 2 628.

### 15.2 User rulings (TG 2026-11-10)

* Case 1: when A has its own nucleus, from **10 %** the piece goes to B
  (A's border moves, the nucleus stays whole); if that would cut A apart,
  the piece is trimmed instead (it stays A's cytoplasm). A with no other
  nucleus: as today.
* Case 2: split, **on by default**, with a switch to turn it off (Fable's
  proposal): a nucleus is a seed when its area is >= 0.5 x the largest
  nucleus in that cell and >= 40 px; a cell with >= 2 seeds is split among
  them; other nuclei (fragments, the uncertain 0.25–0.5) are not seeds.
* Both methods (`cellpose_wholecell_nuclei`, `hybrid_cellpose_stardist`).
* Fix the StarDist crash on a near-blank tile (§15.4).

### 15.3 Rules (in this order; constants fixed, not parameters except the switch)

1. A cell wholly inside one nucleus is removed (unchanged).
2. **Move (new).** On the masks after 1, every measure taken once before any
   move: `own[A]` = A is the unique largest cell of some nucleus m with
   >= 50 % of m in A. For a nucleus n on >= 2 cells, B = the cell with the
   unique largest piece of n (a tie: no move for n); for every other cell A
   with piece >= 10 % of n (`MIN_PIECE`) and `own[A]` by a nucleus m != n,
   the piece is a candidate move A -> B. For each A, all its candidate
   losses are judged together: they are applied only if A minus them has no
   more 8-connected components than A had (else none of them is applied;
   the refused pieces stay in A and are trimmed: like a sliver they become
   A's cytoplasm, not a nucleus of A). A tie for B keeps today's rule 4.
   Order-free: losses are judged without gains.
3. The existing rules 2–4 (nucleus on no cell, union, shared out,
   background) run unchanged on the moved masks.
4. Nucleus-born cells grow 2 px into background (unchanged).
5. **Split (new, the switch, default on).** For each cell C, its kept nucleus
   pieces (one per original nucleus by construction): seeds = pieces with
   area >= `SEED_RATIO` 0.5 x the largest piece in C and >= `SEED_MIN_PX` 40.
   With >= 2 seeds, C (its bounding box crop) is flooded from the seeds
   inside C (`skimage.segmentation.watershed` on a flat image, markers =
   the seed pieces, mask = C, 8-connectivity -- geodesic nearest seed; the
   crop is the same in every window that holds C whole, so the result is).
   Pixels of C the flood does not reach (a part of C cut off from every
   seed, e.g. by a nucleus-born cell taking a bridge) take the label of the
   nearest flooded pixel (`ndi.distance_transform_edt(..., return_indices=
   True)` on the crop), so no pixel of C is lost. A non-seed piece then goes
   whole to the sub-cell holding most of its pixels (tie: the larger seed's)
   and the cell label under every pixel of that piece becomes that sub-cell
   (every piece stays wholly in its cell). The sub-cells are new cells; no
   growth after.
6. A cell without a nucleus is dropped; cells 1..N, pieces 1..K (unchanged).

New counted events (`objects`, owned by centroid, into Step1 `drops`, Step2
`nuclei.pairing`, the Step1 Results line): `nuclei_moved` (per nucleus whose
piece(s) moved), `moves_refused` (per nucleus, A would be cut apart),
`cells_split` (per original cell).

### 15.4 StarDist on a near-blank tile (pre-existing defect, all StarDist methods)

Whole-slide stats crashed the engine process: a 3472² window with 0.12 %
tissue pixels has percentile 1 == percentile 99.8 == 0, csbdeep's
`normalize` divides by ~1e-20, distances reach 1e19 and StarDist's polygon
NMS (ClipperLib, C++) aborts the process ("Coordinate outside allowed
range"). Not the torch port (the Keras path normalises the same). Step2's
"skip tiles without tissue" skips only tissue-free windows, so a Step2 tile
with a sliver of tissue can hit it. Fix in `StardistEngine.predict`: only
when `p99.8 - p1 <= 1e-6`, take the two percentiles over the non-zero pixels
(no non-zero pixel, or still degenerate: an empty label image without
calling the model). Every other tile goes through the identical call, so no
behaviour version changes.

### 15.5 Versions and the Step1 → Step2 contract

The rules change the output of both methods, so `preseg_contract.fixed_rules`
gives both methods `{"pairing_rules": 2}` (`nuclei_pairing.PAIRING_RULES`;
carried in the contract's params like Mesmer's fixed rules; the engine
receives it and ignores it). The value must describe the Step1 result, not
the code that saves it (codex round 1, P2): Step1's `run_job` writes
`pairing_rules` into every record of these two methods, and
`preseg_contract.build` refuses a combination whose successful records do
not all carry the current value (a v1.3 run has none). The same check sits
in `preseg_run.selectable` (codex round 2, P2), which the Use button already
calls before it changes the selection, so Use on an old result says "This
result cannot be used: made with the older cell–nucleus pairing rules; run
the pre-segmentation again" and nothing is selected. The value is written by
`preseg_run.publish_result(..., pairing_rules=)` into the record before it
is published (a new keyword, like `drops`). `validate` refuses an exported v1.3 contract (no
`pairing_rules`) with the same message. The switch `split_cells_by_nuclei` is a method parameter (on / off,
listable in Step1 so both can be compared; default on), part of the
combination like the others; Step2 shows it as a checkbox for both methods.
Engine identities are unchanged (the pairing is not the engine's).

### 15.6 Closed whitelist

* `core/nuclei_pairing.py` -- rules 2 and 5, constants, `split` argument,
  events.
* `core/preseg_contract.py` -- `fixed_rules`, `build` checks the records'
  `pairing_rules`, the refusal message.
* `core/preseg_run.py` -- `publish_result(pairing_rules=)`, `selectable`
  refuses records without the current value (these two methods only).
* `utils/segmentation_param_schema.py` -- a "switch" kind (on / off) and the
  parameter for both methods.
* `utils/segmentation_config.py` -- the default for both methods.
* `ui/step1_presegmentation/run_job.py` -- pass the switch; records carry
  `pairing_rules`.
* (`method_editor.py` needs no change: it formats and parses through the
  schema.)
* `ui/main_window.py` -- the Results line names moved / split.
* `ui/step2_page.py` -- the checkbox (both methods), load / save.
* `workers/segment_merge_worker.py` -- pass the switch.
* `seg_runner/engines.py` -- §15.4.
* `docs/user_guide.md`, `docs/用户指南.md` -- the rules and the switch.
* Tests: `tests/test_cellpose_cell_nucleus.py` (move, refused move, no move
  without own nucleus, tie, split / no split for fragment and uncertain,
  3 seeds, non-seed piece follows, switch off = v1.3 plus move, invariants),
  `tests/test_hybrid_cellpose_stardist.py` (near-blank tile), contract /
  schema / method-plan tests touched by the new parameter and fixed rule;
  a cut-off seedless part keeps its pixels; switch off survives flat and
  nested config round trips (the normaliser's flat keys include it); a v1.3
  record / contract is refused at Use / Step2; a finished v1.4 run reloaded
  from disk can be Used.

### 15.7 Risks

* Real binucleate cells are split when the switch is on (the user's ruling;
  the switch turns it off, per combination).
* Cell counts change (~5 % more cells from the split on this slide);
  v1.3 Step1 results must be run again before Step2.
* A split cell's Step4 features differ from v1.3; that is the intent.

### 15.8 Codex plan review round 1 (astra low) — FAIL (P2 ×2), taken

* P2: `build` stamped the CURRENT `pairing_rules` on an old result → records carry the value, `build` checks it (§15.5).
* P2: a part of a cell cut off from every seed was lost by the flood → nearest flooded pixel (§15.3 rule 5).
* Advisories taken: non-seed piece relabels the cell under it; flat/nested switch-off round trip; `method_editor.py` unchanged.

### 15.9 Codex plan review round 2 (astra low) — FAIL (P2 ×1), taken

* P2: Use called only `selectable`, so the records' check in `build` came late (at Generate) → the check also in `preseg_run.selectable`; `core/preseg_run.py` added to the whitelist (§15.5, §15.6).
* Advisory taken: `publish_result` writes the value before publishing; acceptance "finished → reloaded → Use".

## 16. v1.4 implementation (2026-11-10; user TG: "同意修复…从 10% 开始移动边界…默认开启…两者")

* `core/nuclei_pairing.py`: `PAIRING_RULES = 2`, `SEED_RATIO = 0.5`,
  `SEED_MIN_PX = 40`; `combine_cells_and_nuclei(cells, nuclei, split=True)`;
  rule 1b `_move_straddling_pieces` (measured once on the masks after rule 1;
  a losing cell judged on all its losses, 8-connected parts before / after;
  refused pieces excluded from the eligible pieces = trimmed); rule 4b
  `split_by_seeds` (per split cell: bbox crop, `watershed` on a flat image
  from the seed pieces, 8-connectivity; unreached pixels take the nearest
  flooded pixel via `distance_transform_edt(return_indices=True)`; a non-seed
  piece goes whole by votes, tie: larger seed, then the seed whose first
  pixel comes first in raster order; first sub-cell keeps the id). Events
  `nuclei_moved`, `moves_refused`, `cells_split` in `counts` and `objects`.
* Contract (§15.5): `fixed_rules` → `{"pairing_rules": 2}` for
  `engines.CELL_AND_NUCLEUS_METHODS`; `publish_result(pairing_rules=)`;
  `run_job` writes it; `selectable` and `build` refuse records without it,
  `validate` a contract without it (`OLDER_PAIRING`).
* Switch: schema kind "switch" (on / off, listable), `SPLIT_SPEC` on both
  methods; registry defaults True; normaliser's flat keys
  `split_cells_by_nuclei`, `pairing_rules`; Step2 checkbox "Split a cell
  holding two similar nuclei" (both methods), `get_seg_config` writes the
  switch and the fixed rule, other methods drop them; Step1 `combine_window`
  and the Step2 worker pass the switch to the pairing.
* StarDist (§15.4): `engines.stardist_input` -- the same percentiles and
  `normalize_mi_ma` as csbdeep's `normalize` (a test checks equality), the
  non-zero fallback only when `p99.8 - p1 <= 1e-6`, None → empty labels.
  The real crashing window (3472², 0.12 % non-zero) now runs (2 nuclei).
* Step1 Results line: "… borders moved to a nucleus (R refused) · S cells
  split · …". User guides (EN / 中文) Appendix A rows.
* Real data (user's slide, raw hybrid masks, one 3072² tile, owned by no
  one -- the whole tile): 330 nuclei moved, 5 refused (cut-off parts 1–50
  px), 272 cells split (6 722 → 7 007 cells); 0.8 s, peak ~250 MiB
  (tracemalloc).
* Tests: `test_cellpose_cell_nucleus.py` (+11: move, refused → trimmed, no
  own nucleus, tie, split, switch off, fragment / uncertain, three seeds,
  non-seed piece whole, cut-off part kept), `test_hybrid_cellpose_stardist.py`
  (+12: csbdeep equality, near-blank fallback, StarDist and hybrid survive a
  near-blank tile, switch parse / combos, flat + nested off, page round trip
  and hidden for other methods, the worker passes on / off),
  `test_preseg_contract.py` (+6: an old result cannot be Used / built, a
  reloaded new one can, an old exported contract refused; COMBOS carry the
  switch off).

### 16.1 Codex v1.4 code review

* Round 1 (astra low): FAIL, P2 ×1 — the Results line lacked `moves_refused`; fixed.
* Round 2 (astra low): **PASS**.
