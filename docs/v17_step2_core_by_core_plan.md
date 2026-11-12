# Step2 runs core by core when it follows cores (backlog 15) — plan v0.5

User (TG) 2026-11-11, after accepting backlog 11 stages 2-3: in Step2 a run
that follows the ticked cores still shows the tile grid and still runs
tile by tile; "不论难度多大都应该改为逐core运行，同意拆开做，请生成计划并请codex astra low评估".

"Follows cores" = the run's Regions snapshot has cores: a TMA (`"tma"`) or
cores found without a map sheet (`"cores"`), `analysis.has_cores`.

## 1. Today (read-only survey; full notes in the scratchpad)

* **Grid**: `utils/tile_scheduler.py` `TileScheduler(H, W, rows, cols,
  overlap)` -- equal cells, each read with `overlap` px of halo, row-major
  indices. Rows / cols / overlap come from the page's "Tile Grid" box
  (`ui/step2_page.py:519-555`; rows/cols 1-20, overlap 50-1000, default
  200). A Step1 hand-over requires `overlap == halo_px` (200).
* **Cores today only take tiles away**: `_regions_for`
  (`workers/segment_merge_worker.py:871`) skips every tile whose read
  window holds no selected-core pixel; a tile holding a core edge is
  segmented whole, the rest of it wasted; a core cut by grid lines is
  segmented in pieces and joined by the seam merge.
* **Two loops**: the ROI loop `_segment_one_zarr` (:2874) and the full-slide
  loop in `run()` (:3824); per tile: read the window (`_prepare_tile_payload`),
  one engine task (`_segment_tile_contract`, the npy file in / masks out),
  then `SeamMerger.add_tile` (`core/seam_merge.py`): objects touching an
  internal window edge are dropped, seam candidates spilled, owned interior
  objects written with ids 1..N in write order; the Regions judge
  (`ObjectTally`, needs uncut objects) runs per window; `finish` resolves
  seams. A skipped tile still reads its DAPI so the slide's DAPI has no holes.
* **No per-tile resume** (Stop publishes nothing); Step3 / Step4 never read
  the tile layout (only `label_store.seam_merge` counts and the Regions
  facts, which are by cell id).
* **Engines**: one task = one window loaded whole. StarDist (and the
  hybrid) has no internal tiling: the page caps a read window at 60 MP
  (`STARDIST_MAX_TILE_PX`, a 150 MP window crashed). Cellpose and Mesmer
  tile internally. A TMA core (1.5 mm ≈ 2300-4600 px, + 200 px halo) is
  ≈ 7-25 MP.
* **Page**: the grid is drawn on the Tile Status overview and the zoomable
  viewer (`_draw_tile_grid`, `tile_items`); tile skipping (Skip tiles
  without tissue / Skip all empty / Skip none, clicks, Shift-box); run
  progress coloured per tile; core outlines on top (`CoreOutlines`). The
  Regions snapshot is frozen only at Run.

## 2. Split (the user agreed to two blocks)

* **15A — the run, core by core** (this plan in detail): the worker's unit
  becomes the core; the grid controls and the grid drawing step aside for
  such a run, progress counts cores. Without this the grid stays wrong
  whatever the display shows.
* **15B — the Tile Status view for cores** (outlined in §6, its own plan
  after 15A is accepted): each core coloured by its state, a click shows
  its numbers, the finish dialog's wording.

## 3. 15A design

### 3.0 Which runs, decided from the frozen snapshot
* What runs is decided **after** the Regions snapshot is frozen at Run (the
  manual freeze, `main_window._step2_manual_regions`, or the hand-over
  route, which Step2 also re-freezes from the last Save, `step2_page.py:2184`),
  including after the "no core ticked -- run all eligible?" question: that
  exact snapshot has cores → a cores run; otherwise a grid run as today.
* The page's earlier look (from the last Save, §3.6) is only a preview; if
  the frozen snapshot disagrees, the frozen one wins and the page switches
  its controls and drawing to it before the worker starts.

### 3.1 Units
* A cores run builds **one unit per selected core** instead of the grid,
  in region-local, half-open pixel coordinates: the core's bbox clipped to
  the region (a core partly outside the region keeps its inside part).
  **The read window is that bbox, no halo** (user, 2026-11-11: Find cores'
  outlines already reach beyond the tissue -- `margin_frac`, 6 % of a
  diameter -- so every cell of the core is inside its bbox). An object
  reaching the bbox edge is dropped as at any internal window edge (a cut
  object cannot be judged whole). **Accepted trade-off:** a core drawn by
  hand or edited inwards has no such margin; a cell crossing its bbox edge
  is cut and dropped (counted), where a grid run could have kept it.
  A hand-over's `overlap == halo_px` check (`ui/step2_page.py:3716-3723`,
  `workers/segment_merge_worker.py:3625-3629`) is skipped for a run the
  frozen snapshot makes a cores run (the check moves after the layout
  choice); the contract's format and other checks stay.
* **Ownership = the whole read window**: a unit's `own_bbox` is its read
  window, so every complete object in it is owned. Two units overlap when
  their bboxes intersect (e.g. diagonal neighbours); an object meeting the
  other window is then a seam candidate resolved once, as at a tile seam
  (`is_seam` / `resolve`, unchanged): two whole versions → kept once; one
  version cut at a window edge → dropped, the whole one kept. No
  change to `core/seam_merge.py`.
* **A core too large for one engine call** is split into the fewest equal
  parts of its bbox whose read windows fit: caps on the read area, per
  engine -- StarDist and the hybrid 60 MP (the page's existing hard cap),
  Cellpose 64 MP and Mesmer 35 MP (the largest targets of
  `suggest_tile_strategy` for these backends, used as fixed caps here).
  The part grid is the smallest rows × cols that fits, rows ≤ cols, fewer
  rows on a tie. **Between the parts of one core there is an overlap** (the
  run's overlap, 200 by default; a hand-over's `halo_px`) so a cell on a
  part boundary is whole in one of them -- only inside the core's bbox,
  never beyond it. **Each part owns its whole read window**
  (`own_bbox = read_bbox`); an object meeting another part's window is a
  seam candidate resolved once, as tiles are. Parts run one after the other
  before the next core.
* Every unit and part has a unique index 0..K-1 (cores in snapshot order,
  parts row-major inside), `core_uid`, `part (j, k)`.
* Execution reuses the existing scheduler lifecycle: `TileScheduler` takes
  an explicit list of units (`TileScheduler.from_units`) and keeps its
  payload loading, prefetch and cleanup unchanged.

### 3.2 Merge, judge, nuclei, ids
* The Regions judge (`ObjectTally`) is unchanged: an object belongs to the
  core holding most of its pixels, else `no_core`.
* Nuclei: the seam path carries them as today; the nuclei-drop count counts
  a dropped nucleus in the first unit (in order) whose read window holds
  its centroid, so overlapping windows do not count it twice.
* Ids: 1..N per region in write order, as today (now core after core).

### 3.3 DAPI and what is not segmented
* Nothing outside the units' read windows is read for segmentation.
* **DAPI**: units do not write DAPI; after the units the slide's DAPI image
  is streamed over the whole region in bounded blocks (no engine call), so
  halos and gaps are complete and equal to a grid run's.

### 3.4 Both loops, one rule
* One helper chooses the layout (units or grid) from the frozen snapshot;
  both loops (ROI and full slide) use it; the per-unit body is the per-tile
  body.
* For a cores run: the user's tile skip plan does not apply (ignored and
  recorded as ignored); **Recovery Mode is refused** before any output is
  made (its files are addressed by grid row / column).

### 3.5 Progress, stop, metadata
* Signals stay (`tile_done(idx, n, cells)`, `tile_skipped`, `progress`)
  with the unit index; text "core i of N" (or "core i of N, part j of k"),
  then "DAPI …" for the fill.
* **Stop** is checked before each unit, after inference, inside the DAPI
  fill (each block) and before the outputs are published, in both loops;
  a Stop publishes nothing (the full-slide loop's missing check before
  publication is added). Prefetch cleanup as today.
* Metadata (`segmentation_meta*.json`, the ROI summary, `run_metadata.json`,
  `params.json`): `layout: "cores"` with `units: [{index, core_uid, part,
  own_bbox, read_bbox}]`; `tile_grid` / `overlap_px` / `tile_stats` keep
  their keys (grid values for a grid run; `null` / per unit for a cores
  run); `tile_strategy` says `"not used (cores run)"` instead of grid
  metrics; the Regions record drops `skipped_tiles_without_selected_cores`
  for a cores run. A grid run's metadata is unchanged.

### 3.6 The page (only what 15A needs)
* Preview before Run: the last Regions Save has selected cores (the same
  reading `saved_core_items` does) → the Tile Grid box's **Rows / Cols and
  Auto are disabled** with "Runs core by core (N cores)", the overlap stays
  enabled only as the overlap between the parts of a core too large for
  one call (a hand-over's `halo_px` is used for it), **"Skip tiles without tissue" and
  its buttons are disabled**, **no grid is drawn** (overview and viewer),
  the core outlines are; the StarDist grid check and prompt are bypassed
  (the cap is applied to the units, §3.1).
* At Run the frozen snapshot decides (§3.0); a disagreement switches the
  page before the worker starts.
* Run progress: "core i of N"; per-core colouring is 15B.

Not changed: grid runs (no Regions, `"roi"` exclusions-only runs), the
engines, the seam-merge rules, Step3 / Step4, the Regions snapshot.

## 4. Files (15A, closed list)
* `workers/segment_merge_worker.py` (layout choice, both loops, DAPI fill,
  Stop checks, progress text, metadata, skip plan ignored, Recovery Mode
  refused, nuclei-drop count rule).
* `utils/tile_scheduler.py` (`from_units`), `utils/step2_tile.py`
  (`core_uid`, `part`), a small `core_units(...)` helper (new,
  `utils/step2_units.py`: units, caps, split).
* `ui/step2_page.py` (preview, controls, grid drawing off, StarDist check
  bypass, switch at Run, progress line), `ui/main_window.py` only if the
  frozen snapshot must be handed to the page earlier than today.
* `ui/step2_viewer.py` (no grid items for a cores run).
* Tests (new): `tests/test_step2_core_units.py`; existing loop-parametrised
  Step2 tests must pass unchanged for grid runs.
* This plan.

## 5. 15A tests (synthetic; engine predictions controlled where results are compared)
1. Units: 3 ticked cores → 3 units in snapshot order, region-local
   half-open boxes, read window = the core's bbox (no halo); a core partly outside the
   region; nonzero ROI origins; a core above each engine's cap → the
   fewest parts by the tie rule; unique indices.
2. Both loops (`segment_one_zarr`, `run`), prefetch on and off: one engine
   call per unit (counted), none outside the units; with controlled
   predictions, for Find-cores outlines, the kept cells and their facts equal a grid run's on the
   same snapshot (same set, ids may differ in order).
3. A core-edge cell whose centroid lies outside the core's bbox but most of
   whose pixels are inside: kept -- for a whole core and for an outer part
   of a split core. Two cores whose bboxes intersect: a
   shared object kept once. A region-border object: kept as today.
4. Nuclei: containment and parent table as today; a dropped nucleus in an
   overlap counted once; exclusions (seg-invalid and measurement-only) as
   today.
5. DAPI image complete -- halos, gaps between cores, outside the cores --
   and equal to a grid run's, both loops.
6. Stop during inference, during the DAPI fill and before publication: no
   output published, prefetch closed, both loops.
7. A skip plan with a cores run: ignored, recorded; Recovery Mode with a
   cores run: refused before any output.
8. Metadata: `layout: "cores"`, units, ROI summary, `run_metadata.json`,
   `params.json`, `tile_strategy` "not used"; a grid run's metadata equal
   to HEAD's.
9. Page: preview from the Save disables Rows / Cols / Auto / skip controls,
   draws no grid; StarDist's grid prompt not shown; a hand-over whose
  `halo_px` differs from the overlap is not refused for a cores run (still
  refused for a grid run); a hand-drawn tight core drops a cell crossing its
  bbox edge (counted, the accepted trade-off); the
   frozen snapshot disagreeing with the preview (a Save changed, another
   input, no ticks → all eligible, and the reverse) → the page follows the
   frozen one; without cores the page as today.
10. Existing Step2 tests (the files listed in the survey): no new failures
    against HEAD.

Acceptance (user, Mac): the demo TMA (map sheet and map-less): Step2 shows
no grid, runs "core 1 of 3 …", the cells per core close to the earlier
accepted run's (real inference differs a little with the window), Step4 as
before; a slide without Regions still runs on the grid.

## 6. 15B outline (its own plan later)
* Tile Status for a cores run: each core outline coloured pending /
  running / done / failed (unticked cores grey), the count of cells per
  core on hover / click, the finish dialog lists cores instead of tile
  folders; the zoomable viewer the same.

## 7. Risks and rollback
* A very large core split into parts behaves like today's tiles inside it.
* Very close cores share a seam (handled like tiles).
* Rollback: revert the commit; cores runs go back to the grid with skipped
  tiles.

## 8. Plan review (codex astra low)

Round 1 FAIL (scratchpad `codex_g15_plan_r1.txt`), all taken in v0.2:
* P2: a core-edge cell with its centroid outside the core's bbox would not
  be owned. §3.1: a unit owns its whole read window (no seam-merge change);
  overlapping units resolve as seams.
* P2: DAPI is written per own box. §3.3: streamed over the whole region
  after the units.
* P2: the Regions source is the snapshot frozen at Run (and the no-tick
  question). §3.0: the layout follows that snapshot; the page's look is a
  preview and switches on disagreement.
* P2: the StarDist grid check and Auto's overlap would still act. §3.1,
  §3.6: bypassed for cores runs; the halo kept; caps on the units.
* P2: Recovery Mode reads grid-addressed files. §3.4: refused for cores runs.
* P2: the full-slide loop could publish after a Stop. §3.5: Stop checks in
  the fill and before publication, both loops.
* P3: scheduler lifecycle reuse (`from_units`), exact caps and the split
  tie rule, region-local geometry, metadata places, nuclei counts, and the
  tests (§5) specified.

Round 2 FAIL (scratchpad `codex_g15_plan_r2.txt`): the six P2s confirmed
fixed; one new P2 -- the parts of a split core owned only "their cells
plus the halo inside the core", so an outer-edge cell with its centroid
outside the core's bbox was lost. Taken in v0.3: every part owns its
whole read window; test §5.3 covers an outer part.

Round 3 PASS (scratchpad `codex_g15_plan_r3.txt`), no findings.

v0.4 (user, 2026-11-11): "运行单元不必再往外扩展200像素，因为我们当时在find
cores设计时就留了冗余" -- the unit's read window is the core's bbox; the
overlap remains only between the parts of a split core; the hand-over's
halo check does not apply to cores runs. Everything else agreed ("其余同意").

Round 4 (scratchpad `codex_g15_plan_r4.txt`) on v0.4: one P2 -- a hand-drawn
or inward-edited core has no outline margin, so a cell crossing its bbox
edge is cut and dropped; taken in v0.5 as the stated trade-off of the
user's ruling (no patch that would judge cut objects). P3s taken: overlap
between cores is "bboxes intersect", not "twice the halo"; the overlap
control's wording made consistent; where the hand-over halo check moves.

## 9. 15A implementation (uncommitted, 2026-11-11/12)

User (TG): v0.5 approved ("其余同意").

* `utils/step2_units.py` (new): `CAP_PX`, `engine_cap_px`, `split_grid`,
  `core_units` (one unit per core, bbox clipped to the region, read = own =
  the bbox; parts overlap inside the core only; a core record without a
  stored bbox gets it from its outline).
* `utils/step2_tile.py`: `core_uid`, `part`. `utils/tile_scheduler.py`:
  `from_units` (the same loading / prefetch / cleanup).
* `workers/segment_merge_worker.py`: `_requests_cores_run`, `_cores_run`,
  `_make_scheduler` (both loops), `_part_overlap`, `_unit_label` ("core i
  of N[, part j of k]" in the progress and logs), `_fill_dapi` (after the
  units, both loops; the units no longer write DAPI), `_count_box` /
  `_not_counted_before` (a dropped nucleus counted in the first window
  holding it); Recovery Mode refused and the hand-over HALO check skipped
  for a cores run (before anything is written); the skip plan ignored and
  recorded; a Stop checked before the run is published and registered, in
  both branches; metadata `layout: "cores"`, `units`, `tile_strategy: "not
  used (cores run)"`, `tile_grid: null`, `overlap_px` / `part_overlap_px` =
  the parts' overlap; a grid run's keys unchanged.
* `ui/step2_page.py`: `cores_run_count`, `_apply_cores_layout` (Rows / Cols
  / Auto / skip off, the line "Runs core by core (N cores)…", no grid on
  the overview or the viewer, the skip box also locked while a run is on);
  StarDist grid check, Auto and the hand-over HALO check bypassed for a
  cores run; `_run` freezes Regions right after reading the configuration,
  applies its layout, then runs the layout-dependent checks; the skip plan
  is re-read after the grid can no longer change.
* Tests: `tests/test_step2_core_units.py` (13). `tests/test_f4_step2.py`:
  three tests asserted the grid's tile skipping for a run with cores; now
  they assert one unit and nothing skipped, the ignored skip plan, and the
  same cells as a run without Regions for a core box with a margin (a box
  drawn tight around the tissue cuts the cells at its edge: the accepted
  trade-off).

Code review (codex astra low): round 1 FAIL -- P1 the Stop check came
after publication; P2s: layout-dependent checks before the freeze, the
skip box re-enabled during runs, split-core nuclei counted by the parts'
cells, the recorded overlap differing from the parts'; P3s: per-unit DAPI
writes, `overlap_px`. All fixed. Round 2 FAIL -- P2 the skip plan read
before the StarDist check could change the grid; fixed. Round 3 PASS.

Regression (Mac, one test per process, the changed code vs HEAD across the
38 affected files): no failure is the change's own. The failures common
to both are the known Mac ones (`[run]` variants whose `_run` returns
None; shiboken gc segfaults). `test_step2_no_cache_drop[segment_one_zarr-
regions]` asserted the grid's tile skipping and now asserts one unit, nothing
skipped. `test_f5_step2_viewer::test_the_grid_is_drawn_and_clicked_in_slide_pixels`
crashes (139) in the conftest's `gc.collect()` after the test has passed;
bisected: HEAD with 25 unused dummy methods added to `SegmentMergeWorker`
crashes the same way (with 3-15 or 40 it does not). A latent teardown
problem of a bare `Step2Page` that memory layout sets off, not a 15A defect;
the test is left unchanged (outside the closed list).

Acceptance round 1 (the user, 2026-11-12), two fixes the user asked for:
1. Step2's core colours clashed with the tiles' (yellow "does not run" ~
   the running tile, green "runs" ~ a done tile). Step2 now draws its cores
   in the tile scheme: a core that runs like an idle tile (grey), one left
   out like a skipped tile (purple); during a run the core being segmented
   like the running tile (amber, width 4, translucent fill), back to grey
   once its units are done (the units come from the worker's `_units_meta`;
   finished units are kept apart, so the DAPI fill's progress indices do not
   light a core again); nothing highlighted after the run. Step1 and the
   Regions page keep green / yellow. `_core_source_items` (the Regions
   colours, for the count) / `_core_items` (the look), `_running_cores`,
   `_refresh_core_looks`.
2. After a run had frozen 3 cores, a newer Regions Save (map sheet attached,
   every core ticked) still showed 3: the finished run's snapshot hid the
   Save until another input was shown (regenerating the DAPI input keeps the
   path). The frozen snapshot now shows only while its run is on; otherwise
   the last Save, what a run started now follows. Run end redraws.
Tests: `test_step2_core_units` (+2), `test_f5_core_display` (the frozen
snapshot only while running; the colours).

Acceptance round 2 (the user, 2026-11-12): passed, with one addition: a
core whose units are all done gets the done tile's green border, kept after
the run (as done tiles are) until the next run. `_core_states` (running /
done; the unit -> core list copied into the run's progress record from that
run's own worker, never the one before). Regression (22 files, 470 tests)
clean vs HEAD.
