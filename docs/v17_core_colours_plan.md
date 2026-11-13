# One colour scheme for the TMA cores, Step 0.5 to Step 3 (backlog 28) — plan v0.2

Status: the user approved the scheme (§2) on 2026-11-12, "Done" drawn as
green with a light green fill. Codex plan review round 1 FAIL (two P2: the geometric exceptions and overlaps, now §2.1 and test 3); round 2 FAIL (one P2: run all eligible, now §2.1 and test 7); round 3 PASS. The block
starts after the plan review passes.

## 1. Today: three schemes for one core

| where | file | what the colour says |
|---|---|---|
| Step 0.5 (Regions) | `ui/regions/cores_panel.py` `draw_parts`, `COLOR` | the numbering state: `matched` green `#43a047`, `review` orange `#fb8c00`, `unmatched` yellow `#fdd835` (every core found without a map sheet), a core whose position is QC-excluded red `#e53935`; an unowned piece grey dashed, a left-out (dropped) piece red dashed. Bound cores 3 px, others 1.5 px |
| Step 1, Step 3, the Tissue preview | `core/regions/display.py` `_items` (`saved_items`, `snapshot_items`) | runs or not: green / yellow, 1.5 px |
| Step 2 (overview and the zoomable viewer) | `ui/step2_page.py` `_core_items` | the tile scheme (15A acceptance): ticked waiting grey `#808080`, not ticked purple `#c678dd`, running amber `#ffc832` 4 px with fill `#59ffc832`, done green `#3cc850`, failed red `#dc3c3c` (15B) |

So one core can be yellow in Step 0.5 (found without a map sheet, ticked),
green in Step 1 and grey in Step 2. The user: not acceptable; one scheme
for Step 0.5, 1 and 2.

## 2. The scheme (approved by the user, 2026-11-12)

One meaning everywhere: **is the core analysed?** Green yes, yellow no.
Step 2 adds the run's progress on top; Step 0.5 keeps two numbering
reminders that are not run states.

| core | Step 0.5 | Step 1 / Step 3 | Step 2 |
|---|---|---|---|
| analysed | green `#43a047` | green | green (waiting) |
| not analysed (Run not ticked, QC-excluded, not bound in a mapped document) | yellow `#fdd835` | yellow | yellow |
| being segmented | -- | -- | amber `#ffc832` 4 px, fill `#59ffc832` (as now) |
| done | -- | -- | green, fill light green `#4043a047` |
| failed | -- | -- | red `#dc3c3c` (as now, label "failed") |

* Every core outline 3 px (`overlay.BOUND_WIDTH`) in every step; the
  running core 4 px as now. A selected shape in Step 0.5 keeps its white
  halo.
* Step 0.5 only: an unowned piece grey dashed (not a core yet) and a core
  whose numbering waits for a check (`mapping == "review"`) orange -- both
  reminders of numbering work, shown instead of green / yellow. A
  left-out (dropped) piece: yellow dashed (not analysed; dashed as it is
  no core), no longer red.
* Step 2's tile grid keeps its own colours (grey / amber / green / red /
  purple); only the cores change.

### 2.1 "Analysed" in Step 0.5 versus Step 1 / 2

* Step 0.5 draws the open document by the analysis' selection rules
  without the geometry (`core/regions/analysis.py` `select_cores`
  :102-145, `select_cores_without_map` :146-190): with a map sheet a core
  is green when its position has a donor block, its Run is ticked, it is
  the only core bound there, its mapping is `matched` and its QC is not
  `excluded`; without a map sheet when its own Run tick is on
  (`D.core_runs`). Everything else is yellow (a `review` core is drawn
  orange, §2). A new pure function `display.analysed_uids(doc, study)`
  holds these rules; `select_cores` is not called on every redraw (it
  builds each core's outline and probes the workspace's pixels).
* **Not equivalent to the analysis** (codex plan review round 1): the
  analysis also drops, per workspace, a core whose outline has no area
  ("degenerate"; an ordinary transform can make one) or no real pixel in
  the workspace's region (`analysis.py` :169-177, :273-279), and refuses
  the whole snapshot when ticked cores overlap (:295-297) -- Step 1 then
  draws every core yellow ("nothing can run", `main_window.py`
  :2149-2159). Step 0.5 keeps such cores green: it shows what the user
  ticked for analysis; Step 1 / 2 / 3 show what the analysis of this
  workspace takes. Overlaps are told when a run is started (the
  snapshot's refusal, unchanged); B28 adds no warning.
* **Run all eligible** (codex plan review round 2): with no Run tick on,
  Step 2 offers to run every eligible core (`main_window.py`
  :2309-2320, `SELECT_ALL_ELIGIBLE`). Accepted, the run's frozen snapshot
  takes them: green in Step 2 during the run and in Step 3, while Step
  0.5 and Step 1 (the ticks, the last Save) keep them yellow. Also as
  intended: Step 2 / 3 show what that run analysed. These are the only cases where the two differ, and the
  tests name them.
* Step 1 / 3 and Step 2 keep their source: the analysis of this
  workspace (the last Save, or the run's frozen snapshot), which also
  drops cores outside an ROI workspace's region. So in an ROI workspace a
  core outside the region is green in Step 0.5 (whole-slide document,
  ticked) and yellow in Step 1. That is a real difference (the core is
  not analysed in that workspace), not another scheme; the tooltips say
  "in this workspace" as now.
* An unsaved edit in Step 0.5 shows at once there and reaches Step 1 /
  2 with the Save, as now.

## 3. Changes (closed file list)

* `core/regions/display.py`: `_items` gives every piece `width` 3.0
  (`CORE_WIDTH`, equal to `overlay.BOUND_WIDTH`; no Qt import in core:
  the value is repeated with a test that they match); `analysed_uids`
  (§2.1).
* `ui/regions/cores_panel.py` `draw_parts`: the colour of an owned piece
  is green / yellow by the rule of §2.1, orange for `review`; `width`
  BOUND_WIDTH for every owned piece; dropped pieces yellow dashed; the
  unused `COLOR` entries go. The numbering-state colours are no longer
  used for the outline (the core table keeps its own columns).
* `ui/step2_page.py` `_core_items`: not analysed yellow (`RD.YELLOW`)
  instead of purple; waiting `RD.GREEN` width 3 instead of grey; done
  `RD.GREEN` width 3 with `fill` `CORE_DONE_FILL = '#4043a047'`; running
  and failed unchanged. The docstrings and the TMA button's tooltip (lines
  374-375) stay true (green analysed, yellow not); the tooltip adds
  "amber: being segmented, filled green: done, red: failed".
* `ui/main_window.py`: `TMA_CORES_TIPS["step2"]` (lines 526-528) gets the
  same sentence. No other change there.
* `ui/regions/shapes.py`: the docstring at line 39 ("yellow, as Find
  cores'") corrected (a new core's colour follows its Run tick).
* Tests (synthetic): `tests/test_core_colours.py` (new), and the existing
  colour assertions that the change makes untrue
  (`tests/test_step2_core_units.py`, `tests/test_f5_core_display.py`,
  `tests/test_regions_outline_look.py`, any Regions test asserting the
  numbering colours) updated, nothing else in them.

Nothing else: no worker, scheduler, snapshot or document format change;
the analysis' selection is read, not changed.

## 4. Tests

1. Step 0.5, with a map sheet: a bound ticked core green, a bound
   unticked yellow, a QC-excluded yellow, an unbound core yellow, a
   `review` core orange, an unowned piece grey dashed, a dropped piece
   yellow dashed; every owned piece 3 px.
2. Step 0.5 without a map sheet: a ticked core green, an unticked one
   yellow.
3. The same document, saved: Step 1's items (`saved_items` /
   `snapshot_items`) give the same green / yellow per core as Step 0.5
   for a whole-slide workspace when no core is degenerate and none
   overlap; 3 px. The exceptions, each shown: a degenerate ticked core
   (green in Step 0.5, yellow in Step 1), two overlapping ticked cores
   (green in Step 0.5, every core yellow in Step 1).
7. Run all eligible: a document with no tick, its snapshot built with
   `SELECT_ALL_ELIGIBLE`: `snapshot_items` green for the eligible cores
   (Step 2 during the run, Step 3), `analysed_uids` none (Step 0.5).
4. Step 2: waiting green, not ticked yellow, running amber + fill, done
   green + `CORE_DONE_FILL`, failed red; the tile grid's colours
   unchanged (`TILE_COLOURS` untouched).
5. `display.CORE_WIDTH == overlay.BOUND_WIDTH`.
6. `analysed_uids` equals the uids `select_cores` /
   `select_cores_without_map` select on synthetic documents covering every
   non-geometric reason (not ticked, not bound, bound twice, review,
   QC-excluded, no donor block; map-less ticked / unticked), all cores
   with area inside the slide.

Then the Regions, Step1 core display and Step2 regressions, and the
Mac acceptance (Step 0.5 -> Step 1 -> Step 2 run -> Step 3 on the TMA demo
and on a project without a map sheet).

## 5. Risks and rollback

A look only (colours, widths, one tooltip sentence). Risk: a numbering
state that was told by colour (matched green versus unmatched yellow) is
now told by the table and the labels only; the orange "review" reminder
stays on the outline. Rollback: revert the commit.

## 6. Implementation (2026-11-12)
* `core/regions/display.py`: `CORE_WIDTH` 3.0 on every item;
  `analysed_uids(doc, study)` (§2.1).
* `ui/regions/cores_panel.py` `draw_parts`: green / yellow by
  `analysed_uids`, orange for `review`, every owned piece 3 px, a dropped
  piece yellow dashed; `COLOR` is now `analysed`, `not_analysed`,
  `review`, `piece`.
* `ui/step2_page.py` `_core_items`: waiting green, not analysed yellow,
  done green over `CORE_DONE_FILL`, failed red 3 px, running unchanged;
  the TMA button's tooltip. `ui/main_window.py` `TMA_CORES_TIPS["step2"]`.
  `ui/regions/shapes.py`: one docstring.
* Tests: `tests/test_core_colours.py` (new: the rule against the
  analysis with and without a map sheet, a mapped document without its
  sheet, Step 1's colours and width, the degenerate and overlap
  exceptions, run all eligible, the width constant, the Regions page with
  and without a map sheet); colour assertions updated in
  `tests/test_step2_core_units.py`, `tests/test_f5_core_display.py`,
  `tests/test_regions_outline_look.py`.
Code review (codex astra low): round 1 PASS.
