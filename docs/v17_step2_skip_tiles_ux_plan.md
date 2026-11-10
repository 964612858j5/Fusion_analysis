# v17 Step2 skip tiles: a visible selection and a box select — plan v2 (2026-11-08)

v2: codex plan review round 1 (FAIL on 5 P3, no P1/P2; the three user
decisions untouched) answered — see §11. Round 2: PASS (all five closed,
nothing new). Authorized by the user 2026-11-08 (TG); implemented — see §12.

Source: user request and approval (TG). The first implementation was lost
with the server before it was committed; this plan re-creates it from the
recorded conversation. Nothing of it is in the repository (HEAD `b22cdcd`,
branch v17: the code is still the Shift-pick version of `f68ffa6`).

## 0. What the user asked and already decided

User request: the purple border of a skipped tile is not conspicuous; the
user repeatedly could not tell whether a tile was chosen. The whole tile
should carry a purple shade with a thick purple border, a finished tile's
green border may cover it, and selecting / deselecting several tiles must
get better.

The user approved (record, three decisions):

1. The display as in the mock's middle panel (below, §2).
2. "Covered by the green border" = the draw order: purple first, the
   running yellow and done green borders over it, so a shared edge shows
   green / yellow.
3. Box direction **A**: the tile where the box starts decides — it was
   segmented → every boxed tile is skipped; it was skipped → every boxed
   tile is restored.

## 1. Today (read-only check)

* `ui/step2_page.py:2149-2157` — a tile is a `pg.RectROI`, created with a
  1 px grey pen and clicked through `sigClicked`; every later state change
  sets a 2 px pen (`_set_tile_colour`, `:2480-2487`). Proposed skip `plan_skip` = dashed blue-grey `#5a7896`, a tissue tile the
  user chose `plan_forced` = dashed purple `#c678dd`, a skipped tile during
  the run `skipped` = solid blue-grey. No fill.
* `ui/step2_page.py:2318-2403` — a click toggles one tile; with Shift held
  a tile is only "picked" (cyan `#56b6c2`) and the picks change together
  when Shift is released (an application-wide event filter, `_watch_shift`,
  `_apply_tile_pick`; `_run` applies a pending pick, line 3330). The cyan
  intermediate state is the second reason the user cannot tell what is
  chosen.
* `ui/step2_page.py:2503-2516` `tile_items()` feeds the zoomable DAPI
  picture (`ui/step2_viewer.py`), drawn by the shared `RegionsOverlay`
  (`ui/regions/overlay.py:347-357`): pen 1.5 px, optional `dash`, optional
  `hatch` (a pattern in the item colour, no solid fill). For an item the
  tool controller claimed, the overlay calls `drag(finish=True)`, then
  `click()` if the release is within 4 screen px (Manhattan distance,
  `CLICK_SLOP_PX`) of the press, then `released()`; for an unclaimed press,
  `click()` (no movement, no pan) and `released()` (`overlay.py:235-249`).
  `press()` receives only Ctrl (`:222`). Its click
  controller `_GridClicks` (`ui/step2_viewer.py:26-56`) handles only a
  click; it is built in `Step2DapiViewer.refresh()` (`ui/step2_viewer.py:244`).
* `_begin_run_progress` is defined at `ui/step2_page.py:2445` and called at
  `:3443`.

## 2. Display

* **Skipped tile** (`plan_skip`, and `skipped` during a run): the whole
  tile is filled with translucent purple (`#c678dd` at alpha 0.30) and
  bordered with a **4 px solid** purple line. No more dashes, no more
  blue-grey.
* **Tissue tile the user chose to skip** (`plan_forced`): the same, plus
  diagonal purple hatching. During a run it keeps the hatching: a new run
  state `skipped_forced` for a skipped tile that was in the plan's
  `user_forced` when the run started (recorded once in the run's progress
  record at `_begin_run_progress`; nothing else is added to that record).
* **Draw order** (both views): idle grey → purple tiles → running yellow /
  done green / error red → the box preview's white frame. Widths: the
  purple border is 4 px and the white frame 2 px in both views; every other
  state keeps today's width (overview: 1 px at creation, 2 px after a
  state change; zoomable picture: the overlay's default 1.5 px — those
  items carry no `width` key).
  Overview: the tile's z value follows its layer. Zoomable picture:
  `tile_items()` returns the items in that order.
* **Overview tiles** draw fill and hatching through a small local subclass
  of `pg.RectROI` in `ui/step2_page.py` (fill and pattern under the
  outline; the hatching in screen pixels so its density does not change
  with zoom). Pens stay cosmetic (screen pixels).
* **Zoomable picture**: `tile_items()` adds two OPTIONAL keys, `fill`
  (colour with alpha) and `width` (pen px); `hatch` already exists.
  `RegionsOverlay._paint_static` paints, in order, the `fill` (when
  present), the `hatch` (as today) and the border, with `width` when present
  and today's 1.5 / 3.0 (selected) otherwise. Purple items carry
  `width: 4.0`, the preview frame `width: 2.0` and no fill. Regions never sets either key, so
  its drawing is unchanged (tested).
* The Tile Status overview and the zoomable DAPI picture look the same.

## 3. Selection

1. **Click**: toggles the tile immediately (as today). The Shift-pick
   branch, the cyan `picked` state, `_tile_pick`, `_watch_shift`, the
   application-wide event filter, `_apply_tile_pick` and its call in `_run`
   are removed.
2. **Shift + drag a box**, in both views:
   * the press must be on a tile, with Shift held, the option ticked, a plan
     for the drawn grid and no run computing — otherwise the press is not
     taken and pan / zoom / click behave as today;
   * the boxed tiles are the rows × columns spanned by the start tile and
     the tile under the pointer (outside the grid: clamped to its edge);
   * while dragging, the boxed tiles show a white 2 px frame and already
     look like the result (purple or not); the drawing is refreshed only
     when the boxed range changes, not on every mouse move;
   * on release the boxed tiles are SET (not toggled) in the start tile's
     direction (decision 3). Tiles already in that state are left alone.
     Restoring asks nothing. Skipping: empty tiles are skipped; tissue tiles
     in the box are asked about **once** (today's single-tile question for
     one, today's list question for several); No keeps the tissue tiles
     segmented and still skips the empty ones (today's multi-pick rule);
   * **Shift + click** without moving (≤ 4 px, the overlay's own click
     slop) toggles just that tile, like a plain click;
   * Shift is needed only at the press (read through the existing
     `_shift_held()` there); releasing it mid-drag does not end the box.

   **One commit per gesture.** One transient record holds a box in progress
   (start tile, current tile, direction) — the gesture's own state, nothing
   persistent. Page functions: `_box_begin(r, c) -> bool` (checks the press
   conditions above, records the box), `_box_move(r, c)` (redraws only when
   the boxed range changed), `_box_click()` (Shift + click: clears the box,
   toggles the start tile once), `_box_finish()` (clears the box, sets the
   boxed tiles once), `_box_cancel()` (clears it, redraws, no change), and
   `tile_at_clamped(y, x)` (the tile under a point, clamped into the grid).
   Whichever of click / finish / cancel runs first clears the record, so the
   callbacks that follow it in the same release find no box and do nothing.

   * **Zoomable picture** (`ui/step2_viewer.py`): `_GridClicks(tile_at,
     on_tile, box=None)` — the new optional `box` is the page, passed by
     its one construction in `Step2DapiViewer.refresh()`; without it the
     controller behaves exactly as today (the existing tests build it so).
     `press(y, x, ctrl)`: a tile under the point and `box._box_begin(r, c)`
     true → claimed (True); otherwise False as today. `drag(y, x,
     finish=False)`: with a box, `_box_move(*tile_at_clamped(y, x))`; the
     finishing call only moves, it never commits. `click(y, x)`: with a box,
     `_box_click()`; without one, `on_tile` as today (the unclaimed plain
     click). `released()`: with a box still recorded, `_box_finish()`. The
     shared `RegionsOverlay.eventFilter` is not changed.
   * **Overview** (`ui/step2_page.py`): an event filter on the overview's
     own viewport (`self._ov_gv.viewport()`), which sees mouse events before
     the scene. A Shift + left press for which `_box_begin` is true is
     consumed together with its moves and its release (the RectROI never
     receives them): moves → `_box_move`; release beyond 4 screen px
     (Manhattan, the overlay's rule) → `_box_finish()`, otherwise
     `_box_click()`. Any other press is not consumed: pan / zoom and the
     plain click through `RectROI.sigClicked` work as today, so a plain
     click toggles exactly once, by one path.
3. **A box in progress is dropped** (no change, frames gone) when Rows /
   Cols / Overlap change, the option is unticked, a run starts, or one of
   the two buttons below is pressed (`_box_cancel`; the release that
   follows then does nothing).
4. **Two buttons** in one row under the checkbox: `Skip all empty` (back to
   the automatic proposal: the user's own changes are cleared) and
   `Skip none` (every tile is segmented). No question for either. Shown
   with the count line (the option ticked and a plan available); disabled
   while a run computes. Plain `QPushButton`s in the page's existing
   button style.
5. The count line stays: `X of Y tiles skipped (...) — click a tile to
   change it, or hold Shift and drag over several`. The checkbox tooltip
   says the same.

## 4. What does not change

The worker and `seg_config["skip_tiles"]` (same shape), the tissue
proposal (`core/tile_tissue.py`), what a run records, the overview picture,
the grid geometry, the TMA core outlines, Regions, the viewer's reading /
scheduling / caching. Running yellow, done green and error red keep their
colours and widths.

## 5. Scope (closed whitelist, AGENTS.md rule 1)

* `ui/step2_page.py` — only the skip-plan display and selection: tile
  colours / layers / the RectROI subclass, `tile_items`, `_on_tile_clicked`
  / `_toggle_tiles` (+ the set-in-one-direction helper), removal of the
  Shift pick, the box functions and the overview's viewport filter, the two
  buttons, the count line and tooltip, `skipped_forced` in
  `_begin_run_progress` / `_record_tile` / `_on_tile_skipped`, the line in
  `_run`.
* `ui/step2_viewer.py` — `_GridClicks`, and passing `box=self.page` at its
  one construction in `Step2DapiViewer.refresh()`. Nothing of the viewer's
  reading, scheduling or caching.
* `ui/regions/overlay.py` — `_paint_static`: the optional `fill` and
  `width` keys only.
* `tests/test_step2_skip_empty_tiles.py` — the four Shift-pick tests are
  replaced; the existing assertions of the old blue-grey look (`:317`,
  `:353`, `:357`) are updated to the purple look; new tests below.
* `tests/test_f5_step2_viewer.py` — the box through `_GridClicks`; the
  items' new keys and order.
* `tests/test_regions_overlay_cache.py` — Regions items paint unchanged.
* `UI_SURFACE_RULES.md` — the Step2 sentences on skipped tiles (dashed
  blue-grey / purple, solid blue-grey during the run) and the new gesture
  and buttons.
* `docs/user_guide.md`, `docs/用户指南.md` — the skip paragraph.
* this plan.

## 6. Tests (offscreen, public operations; seams that already exist:
`_shift_held`, `_confirm_forced_skip(s)`)

* Look: a proposed tile's pen is purple, 4 px, solid, with the purple fill;
  a forced tile also has the hatch; during a run a skipped tile is purple
  and a forced one keeps its hatch; running / done tiles sit above purple
  ones (overview z values; zoomable item order).
* Click: one click skips / restores at once; no cyan state exists.
* Overview Shift-drag (real mouse events on its viewport): from a
  segmented tile → all boxed tiles skipped; from a skipped tile → all
  restored; tissue tiles in the box → one question; No → tissue tiles
  segmented, empty ones skipped; white frames during the drag and gone
  after; Shift-click toggles one tile; without Shift the press pans as
  before (no change to the plan).
* Zoomable picture, through a real `RegionsOverlay` on a pyqtgraph view
  with `_GridClicks(..., box=page)` and mouse events sent to its viewport
  (not only direct controller calls): Shift + click toggles once; a box
  commits once and asks once; a plain click toggles once.
* Both views: releasing Shift mid-drag still commits the box; dragging out
  of the grid clamps to its edge tiles; moving inside the same boxed range
  does not redraw (redraw count); a box over mixed tiles SETS them in the
  start tile's direction and leaves the tiles already in that state alone;
  a box cancelled mid-drag commits nothing at its release.
* Painting, not only properties: the overview and the overlay rendered to
  an image — inside a purple tile the purple fill, a forced tile's hatch
  lines, a done tile's green on the shared edge, the white preview frame.
* Buttons: `Skip all empty` restores the proposal and clears the user's
  choices; `Skip none` leaves no tile skipped; disabled during a run;
  hidden when the option is off.
* A box in progress is dropped by a grid change, by unticking, by a run
  start and by either button.
* Regions overlay: an item without `fill` / `width` paints exactly as
  before (no fill inside, pen width unchanged); with `fill` the inside is
  coloured.
* Mutation: the old drawing / the old Shift pick make the new tests fail.
* Regression (targeted): `test_step2_skip_empty_tiles`,
  `test_f5_step2_viewer`, `test_step2_tile_status`,
  `test_v16_pa_step2_overview`, `test_step2_layout`, `test_f4_step2`,
  `test_regions_overlay_cache`, `test_regions_r1`–`r3`,
  `test_regions_annotate_blocker`, `test_f5_regions_camera / panels /
  scope`, `test_f5_core_display`, `test_tma_f3_page`.

## 7. Test environment (needs the user's choice)

The server with `fusion_test2` (PySide6 6.11.3) was the test and
acceptance machine. This Mac has no Python environment with PySide6 /
pyqtgraph (checked: `a2t`, `fusion-step5n`, system python). Sample data for
a real run is at `/Users/user/Documents/Codex/2026-11-08/ni/work/data/
block01/cropped_region.ome.tif` (the user, 2026-11-08). Either the server
is back, or a local environment is created on this Mac from the repo's
`envs/` files with micromamba (network access; a macOS GPU path is not
verified — the zoomable DAPI picture may fall back to the plain overview
there).

## 8. Real-machine acceptance (user)

Tick `Skip tiles without tissue`: is the purple conspicuous; one click
skips / restores at once; Shift + drag in both the small overview and the
zoomable picture; a box over tissue tiles asks once; the two buttons; run
the slide and check that green / yellow borders sit over the purple and
the hatching stays.

## 9. Risk and rollback

One commit, reverted alone. The overlay change is off unless an item sets
the new keys. The worker's input is unchanged, so a run is the same for the
same chosen tiles.

## 10. Assumptions to confirm (advisory)

* The buttons appear only while the option is ticked (like the count line).
* Fill alpha 0.30; the hatch in the same purple.

## 11. Codex plan review round 1 — answers

* P3 zoomable wiring outside the whitelist → `_GridClicks` gets an optional
  `box` (the page), passed at its one construction in `refresh()`; both in
  the whitelist (§3.2, §5).
* P3 overlay call order / one commit → the callbacks' roles are fixed
  (`drag(finish)` only moves, `click` = Shift + click, `released` commits
  what is left); the first of click / finish / cancel clears the record;
  Shift read by `_shift_held()` at the press; the slop is 4 screen px,
  Manhattan (§3.2).
* P3 overview filter vs RectROI → a claimed Shift gesture is consumed whole
  by the viewport filter; everything else goes to RectROI as today, one
  path per click; tested (§3.2, §6).
* P3 facts → RectROI creation `:2149`, 1 px at creation / 2 px after a
  change, 1.5 px in the overlay; the widths are stated per state (§1, §2).
* P3 tests → real overlay event chain, Shift released mid-drag, clamping,
  no redraw within the same range, SET over mixed tiles, cancelled box,
  painted pixels; the old blue-grey assertions are updated (§5, §6).

## 12. Implementation notes (authorized 2026-11-08)

* As planned (§2, §3, §5). A tile under the box shows its RESULT look
  (purple 4 px, fill, hatching for a tissue tile) with the white 2 px frame
  drawn over it: overview through `_TileRect.frame`, painted after the
  outline (RectROI.paint scales the painter to a unit square, so it is
  wrapped in save / restore); zoomable picture through an extra item
  `tile_r_c_box` after the tile's own item. `_GridClicks` remembers a press
  it took (`_claimed`): when the box was dropped before the release, the
  release does nothing (not a plain click).
* Test environment (§7, user's choice): a local env on the Mac mini,
  `work/envs/block01_mac` (Python 3.10, `envs/ci/requirements-cpu.txt`:
  PySide6 6.11.3, pyqtgraph 0.14.0), offscreen. On this Mac, unchanged HEAD
  `b22cdcd` already shows (checked in a HEAD worktree): a shiboken6
  segfault in the shared conftest's between-test garbage collection, a
  failure of `test_skipping_empty_tiles_changes_no_output[run]`, and wait
  timeouts in `test_regions_r3` / `test_tma_f3_page`. Tests were therefore
  run one process per test, 4 in parallel, and compared with HEAD.
* Results: skip tiles + zoomable viewer + overlay + tile status + layout +
  overview files — 107 passed, 2 skipped, 1 failed (the HEAD failure above).
  Wider regression (f4 step2, regions r1–r3, annotate blocker, f5 regions
  camera / panels / scope, f5 core display, tma f3 page): no failure that
  HEAD does not have. Mutations (old look, no zoomable box, no overview
  box, double commit, overlay without fill / width, preview without hatch,
  no `_claimed`, no save / restore) each fail their tests.
* The page fixture of `test_step2_skip_empty_tiles` waits for the
  overview's background read before a test (a page deleted while it runs is
  a test artefact).
* Codex code review round 1: FAIL (P2 preview without the result look; P3
  release after a dropped box in the zoomable picture; P3 coverage) — all
  applied. A rendered check then found the frame drawn far beyond its tile
  (the painter scale above): fixed, with a pixel test. Round 2: PASS.
* Real-machine acceptance (§8): pending, by the user. Not committed.

## 13. Acceptance round 1 changes (user, TG 2026-11-09)

The user's real-machine check on the Mac mini (purple: conspicuous enough)
asked for two changes, to be fixed before the segmentation run:

1. **Ticking skips nothing.** Ticking "Skip tiles without tissue" must not
   assign any tile as skipped. The proposal is still computed (the count
   line names how many tiles have no tissue), but every tile starts
   segmented; "Skip all empty" applies the proposal, "Skip none" clears.
   A grid change resets to "nothing skipped" (the note only when the user
   had skipped something).
2. **Shift collects, one question at the end.** While Shift is held,
   clicks and boxes only collect: each touched tile shows its result look
   framed in white, nothing is committed and nothing is asked. When Shift
   is released (and no box is being dragged), everything collected is
   committed at once, with ONE question listing the collected tiles that
   hold tissue (No = those stay segmented, the rest is applied). A second
   Shift-click on a collected tile takes it back out. A plain click while
   tiles are collected first commits them. Grid change, unticking, a run
   start or either button drop the collection without change. Plain clicks
   are unchanged (immediate, a question for a tissue tile).
   Box direction is still the start tile's (decision A), taken from its
   collected state when it has one.

Shift release is detected by a 100 ms check of the keyboard state that runs
only while tiles are collected (a key release can be lost when the window
loses focus). Same files as §5; tests in the same modules; UI_SURFACE_RULES
and both user guides updated.

The user also reported that Shift+drag did not work for them; not
reproduced offscreen — asked the user where (thumbnail or zoomable view)
and what happened.

Code review of §13 (codex astra low, 2026-11-09), round 1: FAIL, taken —
P2 the Shift check read the last event's modifiers (`keyboardModifiers`),
so Shift let go in another window was never seen: now the live state
(`queryKeyboardModifiers`); P3 the check kept running after the last
collected tile was taken back out: stopped; P3 the tests called the check
directly: a test now lets the real 100 ms timer fire (not while a box is
held, commit once Shift is up). Ticking skips nothing and the Shift rule:
mutation-checked (each old behaviour put back turns its test red).

Shift + drag in the zoomable picture (user, 2026-11-09: "did not work",
tried on Step2's Tile Status, where the zoomable DAPI picture replaces the
overview once a slide is loaded). Reproduced the path in the real app on the
Mac (a copy of the user's test project, paths rewritten, scripted events):
the press reaches the grid controller, but whether a box starts was decided
by a REMEMBERED keyboard state, not by the press. Now the press's own
modifiers decide: `RegionsOverlay` keeps `press_modifiers` (the system's
modifiers sent with the press; no change for other controllers), the grid
controller passes `shift=` to `_box_begin`, and the overview's filter passes
the event's Shift. Shift release (commit) still reads the live state.
Real app after the change: the box starts with `shift=True` and follows the
drag. Test: Shift on the press with the global state saying no starts a box
in both pictures; global yes with no Shift on the press does nothing;
mutation (controller ignores the press's Shift) turns it red.
Code review (codex astra low, 2026-11-09): PASS, no P1/P2; Regions'
controllers do not read the new field (`press(y, x, ctrl)` unchanged).
User real-machine acceptance on the Mac: PASSED (TG 2026-11-09), except
§8 item 5 (a run: green / yellow over purple), which waits for the Cellpose
Mac GPU block's segmentation run.
