# Step2 core status for a cores run (backlog 15B) — plan v0.3

Status: the user chose (A) for the count and approved execution once codex
passes (2026-11-12). Codex plan review round 1 FAIL (the count, fixed in
v0.2). Follows 15A
(`docs/v17_step2_core_by_core_plan.md`, pushed as 0d4e9e6).

## 1. Where 15A left it

15A's acceptance already brought the core of the 15B outline (§6 of the
15A plan): in Step2 the cores use the tile colours, on the overview and on
the zoomable viewer:

| core | look |
|---|---|
| ticked, not run yet | grey (idle tile) |
| not ticked | purple (skipped tile) |
| being segmented | amber, width 4, translucent fill (running tile) |
| all its units done | green (done tile), kept after the run until the next run |

What the outline still lists: a failed state, the cells per core, the
finish dialog naming cores rather than tile files.

## 2. What 15B adds (the page only)

### 2.1 Failed and stopped
* A run that ends with an error: the core(s) being segmented then turn red
  (the tiles' unused `error` colour `#dc3c3c`, width 3), kept after the run
  until the next run, like green. The cores already done stay green; the
  others stay grey.
* A Stop (the worker reports it as the error "Stopped by user."): the core
  being segmented goes back to grey (not done, not failed).

### 2.2 Labels; no cell count (the user's choice A)
* A failed core's label: `failed` (with a map-sheet name: `X1-Y06 D12 ·
  failed`). Other cores keep their label (a name, or none).
* The label is the overlay's existing label: drawn once the core is large
  enough on screen (60 px, as the names now). No hover or click handling
  (the outline's "hover / click"): the outlines stay read-only, nothing new
  catches the mouse.
* **The count is not exact from the page alone** (codex round 1, P2): a
  unit's `tile_done` carries its interior cells only; cells that touch
  another unit's window wait for the seam decision at the end
  (`core/seam_merge.py` `add_tile`, `is_seam`). A core in one unit that no
  other core's window overlaps: exact. A split core: the cells on the
  overlaps between its parts are missing -- for a large core, possibly
  hundreds. So (the user's choice A, 2026-11-12) Step2 shows no cell
  count per core; the finish dialog lists the cores without counts.
  Step4's per-core table (`uns["cores"]`, `n_obs`) is the exact count.

### 2.3 The finish dialog
For a cores run, instead of the tile file lines:

```
Cores: 3 run (4 units)
Per-unit outputs:
  tile_masks/
    tile_r<k>_c0_dapi.ome.tiff / _raw_mask.ome.tiff  (unit k;
    segmentation_meta.json "units" names its core)
```
and the per-core list (name or "core k of N", its state) in the dialog's
"Show Details…" (a long TMA does not stretch the dialog). A grid run's
dialog is unchanged.

## 3. Not in 15B
* States for a run opened later (after a restart, or another workspace and
  back): as for tiles, the states live with the run on screen only.
* File names of the per-unit outputs (unchanged: Step3 / Recovery read
  them).
* Step1 and the Regions page colours (green / yellow, unchanged).
* Cell counts per core in Step2 (choice A).
* The worker, the scheduler, any new signal: none changed.

## 4. Files (closed list)
* `ui/step2_page.py`: `_core_states` gains `failed`; `_core_items` labels
  (`failed`); `_on_error`
  marks the running units failed (not on a Stop); `_on_finished`'s dialog
  for a cores run.
* `tests/test_step2_core_units.py`: the tests below.

## 5. Tests (synthetic, the worker's signals emitted by hand)
1. An error during core 2: core 1 green, core 2 red with `failed`, core 3
   grey; kept after the run; a new run greys them all.
2. A Stop during core 2: core 2 grey, core 1 green.
3. The `failed` label with and without a name.
4. The finish dialog text for a cores run (cores, units, details list) and
   unchanged for a grid run (the dialog is read without showing it).

## 6. Risks and rollback
The page only; a look and a dialog text. Rollback: revert the commit.

## 7. Codex plan review (astra low)
Round 1 FAIL -- P2: per-unit counts are interior cells only (seam cells
decided at the end), not "a few" off. The user chose (A): no count in
Step2. Round 2 PASS.

## 8. Implementation (2026-11-12)
* `ui/step2_page.py`: `_core_states` gains `failed`; `_core_items` draws
  it red (`TILE_COLOURS['error']`, width 3) with `failed` appended to the
  first piece's label; `_mark_failed_cores` (called by `_on_error` before
  the run is let go; not on "Stopped by user."); `_cores_finish_text`
  (the dialog's lines and its "Show Details…" list; None for a grid run).
* `tests/test_step2_core_units.py` (+4): an error (with and without a
  name), a Stop, the dialog for a cores run and None for a grid run. The
  real `_on_finished` text was also checked once with `exec` patched out
  (cores and grid).
Code review (codex astra low): round 1 PASS.
