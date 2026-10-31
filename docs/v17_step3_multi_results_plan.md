# Step3: several results at once, in one floating window (plan v2.1, 2026-10-30)

v1 was reviewed by codex (`codex_s3m_plan_1.out`, verdict "revise", 8 P2s). This version answers each P2 (§7) and takes in the user's rulings.

## 1. User rulings (2026-10-30)

1. Step3 shows several segmentation results at once, on the one viewer.
2. ONE floating window lists ALL result records. Each record has its own cell-mask and nucleus-mask switches, like Step1's Pre-segmentation Results rows.
3. **Colours:** a record's cell mask and nucleus mask each have their own colour. Dashed outlines are wanted; codex and Fable were consulted (§4).
4. **`Use`:** each row has a `Use` button, as Step1. `Use` = this record's masks go to Step4. Only one record at a time.
   - The ticks only decide what is drawn.
5. **No limit** on how many records are drawn at once.
6. **Outline only.** No fill mode at all. When results overlap and cannot be told apart, that is acceptable: it discourages switching on too many.
7. **Drawing order is by record:** one record's masks over another's.
8. **Default:** when Step3 opens, only the active (`Use`) record's cells are on.

## 2. What is there now (survey, verified by codex)

**Step3 shows one result at a time.**
- The run drop-down and `Load…` sit in the Viewer tab-bar corner (`ui/step3_mask_bar.py:314`).
- `Cell mask ▾` / `Nucleus mask ▾` and the hint line sit in the viewer's tool row.
- Each `▾` opens a `MaskPanel` (`step3_mask_bar.py:108`): Show, 7 preset colours + `Custom…`, Opacity, Width 1–4, Outline/Fill.
- Today both kinds are visible by default (`step3_label_binding.py:58`).

**The choice is the project's active segmentation run.**
- It is set with `ProjectState.choose_segmentation_run(..., origin="step3")` (`main_window.py:2508`).
- The run's TMA cores follow it (`main_window.py:2294`), and so does Step4 (`_step4_choice`, `main_window.py:5715`).

**`_step3_refresh_masks`** (`main_window.py:2462`):
- lists runs: `step3_masks.list_project_runs`, current workspace first (`step3_masks.py:284`), plus the `Load…`ed ones;
- chooses ONE entry;
- resolves `{cell, nucleus, reasons}`;
- hands them to `mount.set_mask_sources`, compared by `_step3_source_key`, which sorts kinds.

**Deleting any listed run calls `_step3_clear_masks()`** (`main_window.py:11295`), which clears the active choice and all loaded runs. The `×` deletes the whole run and what was made from it.

**The mount** (`step1_viewer_mount.py:632–720`): one label binding, sources and styles keyed by kind. Styles are applied before sources.

**Bindings.**
- `CoreLabelBinding` (`step3_core_labels.py`):
  - one pyramid-build thread per kind, with one writer per kind (`_running`);
  - nothing is handed to the core while any pyramid builds (`:84`, `:100`);
  - the whole set falls back to L1 when the core cannot read a source.
- L1 `Step3LabelBinding`: one serial worker, pyramid jobs before tile jobs.

**Pyramid builds** (`core/label_pyramid.py:139`, `step3_masks.py:666`):
- a fixed `<output>.partial` directory, removed on entry and on failure;
- two writers to one destination are unsafe;
- a second caller with an old `MaskSource` does not pick up another caller's finished pyramid.

**Rust core.**
- Fixed at two kinds:
  - `label_source.rs:13` `KINDS`;
  - `label_pool.rs:21` `Sources = Arc<[_; 2]>`;
  - `label_plan.rs:24` `shown/pyramid: [bool; 2]`;
  - `viewer/labels.rs:43` `styles: [Style; 2]`;
  - `py/viewer.rs:316`: takes a list, silently uses the first two; the style index is clamped to 1.
- A new source set is a new generation: every held tile goes (`viewer/labels.rs:70`).
- The planner groups requests by kind before cutting to the budget, so a later kind can get nothing (`label_plan.rs:103`).
- The drawing already takes any number of layers (`label_store.rs:43`, `render/labels.rs:76`). Each layer is one id-image pass plus one draw pass.
- `readback_label_ids` returns the last layer's id image; only tests and probes use it.

**Step1 Results row** (`results_panel.py:22`): title, `Use`, parameters, status, Cells/Nuclei ticks, one colour, and `▾` (width, dashed). Above the rows, `ResultsPanel` has tri-state "all" ticks.

**Odon** (GPL, design only):
- Mask layers in a side list, each with visible, opacity, width, display mode and colour (`odon/src/masks/layers.rs:64`).
- Only one segmentation label raster at a time (`app.rs:7155`).
- Same idea for the per-layer settings and for many layers in one list. The floating window is the user's ruling.

## 3. The design

### 3.1 The window

- **Opening it.** A `Results…` button in the Viewer tab-bar corner, where the run drop-down is now, opens ONE non-modal floating tool window (`Step3ResultsWindow`, `Qt.Tool`). It stays above the main window and can be moved anywhere.
- **Closing it** only hides it: what is ticked stays drawn. Its position and size are kept for the session.
- **Top of the window:**
  - `Load…`, moved here from the corner;
  - "all Cells" / "all Nuclei" tri-state ticks, as Step1. With no limit (ruling 5), a click from mixed or off ticks every row that has that mask.
  - The hint line, moved here, for the viewer-wide messages: memory full, no GPU, no runs.
- **Rows:** one per entry (a run and a region, as today's drop-down items), in today's list order (current workspace first, `step3_masks.py:284`), scrolling.
- **Each row:**
  - Title as today's drop-down text, the upstream tag, and `×` where deletion is allowed today.
  - `Use`: the active record (ruling 4). The `In use` row has Step1's green frame. `Use` does not tick or untick anything.
  - `Cells` and `Nuclei` ticks, greyed when the record has no such mask.
  - After each tick, a swatch button that opens that mask's panel: 7 presets + `Custom…`, Opacity, Width 1–4, `Solid / Dashed` (§4).
    - It is today's `MaskPanel` with `Show` (now the tick) and `Fill` removed (ruling 6), and `Dashed` added.
  - A one-line status for this row only:
    - "zoomed-out masks being prepared";
    - "zoomed out: cannot be shown (…)";
    - "not readable: …";
    - "part of this result lies outside the current ROI".
- **Default colours** are given ONCE per entry key, in the order entries are first seen in the session. A new record never recolours an older row.
  - From Step1's palette (`mask_layers.PALETTE`): cells = `PALETTE[2i]`, nuclei = `PALETTE[2i+1]`.
- **Default ticks** (ruling 8): the first time Step3 shows the active record, its Cells is ticked. If that record has no cell mask, its Nuclei is ticked. Nothing else is ticked.
- **What is kept, per entry key, for the session:** ticks and styles survive a list refresh, a Step2 run finishing, closing and reopening the window, and a workspace change, as long as the entry is still listed.
  - A dataset change clears them, as today's `_step3_clear_masks`.
- **`Load…`** adds the run to the list and ticks its Cells (it is what the user just asked to see). It no longer makes it the active record; that is `Use`.
- **`×`** deletes the run as today: one confirmation listing what goes downstream. Only the rows of the deleted runs leave the list and the picture.
  - The active record is cleared only if it was deleted.
  - Loaded runs that were not deleted stay.
  - `_rm_release_paths` gets a Step3 branch that drops only those rows, instead of `_step3_clear_masks()`.

### 3.2 The viewer

- **Every ticked mask of every row is drawn.**
- **Order (ruling 7):** rows from the bottom of the list up, so the top row lies on top. Within a row, cells then nuclei.
  - "Nucleus on top" holds within a record; another record's cells may cover it.
  - Drawing order is part of the source comparison (it no longer sorts kinds).
- **Outline only.** The core keeps its fill code; Step3 never asks for it.
- **TMA cores over Step3** follow the `Use` record, as they follow the drop-down today.
- **ROI clipping** of masks stays as today, so a result from another region is cut at the current ROI. The row status says so.

### 3.3 Slots

A drawn mask is a **slot**: `(entry key, kind)`. Slots are ordered as in §3.2.

**Mount** (`Step1WholeSlideMount`):
- `set_mask_slots([(slot_key, MaskSource, style), ...])` replaces `set_mask_sources` / `set_mask_style`. Styles come WITH the sources, so a new slot never draws with another slot's style.
- `set_slot_style(slot_key, ...)` for a later change.
- `mask_status()`: per slot, plus the viewer-wide part.
- Only Step3 calls these (survey), so the two-kind calls go.

**`CoreLabelBinding`:**
- **Pyramid builds are keyed by destination** (the pyramid's real path), not by kind or slot.
  - One build per destination at a time, as `_running` per kind does today.
  - Every slot whose mask has that destination receives the finished `MaskSource`.
  - A slot that is unticked while its build runs: the build goes on if another slot needs it, otherwise it is cancelled. A later re-tick waits for the old writer to end, then builds again: the existing restart-after rule, now per destination.
  - Pause, resume and L1 transitions keep this rule.
- **Ready slots keep drawing while another slot's pyramid builds** (codex P2-6).
  - A building slot is withheld from the core until its build ends, then added.
  - The slots already shown keep their tiles (§3.4).
  - Today's "nothing is read while a pyramid builds" becomes "that slot is not read while its pyramid builds".
- **L1 fallback stays whole-set**, as today: rare (memory-only pyramid or a layout the core refuses). L1 is generalised to slots with its one serial worker.

**Python-side numbering:** each slot gets a **stamp**, a number that changes whenever that slot's source changes (a new run, a finished pyramid). The core identifies a slot's tiles by its stamp.

### 3.4 Rust core changes (no new module, thread or cache)

- **`Sources`:** an ordered `Vec` of `(stamp, KindSource)`; **styles:** a `Vec` in the same order.
  - `set_label_slots([(stamp, description, style), ...])` replaces `set_label_sources` / `set_label_style`.
  - `set_label_slot_style(stamp, style)`.
- **Tile key:** `LKey = (stamp, level, tx, ty)`, replacing `(generation, kind, level, tx, ty)`.
  - A new slot list keeps every held tile whose stamp is still in it, and releases the rest.
  - So adding or removing one record does not blank the others.
  - The label thread drops jobs whose stamp left the list, as it drops old generations today.
- **Planner fairness** (codex P2-2): the wanted list is built tile-major, slot-minor. For each visible tile, nearest the centre first, every shown slot gets it; then the ring the same way.
  - Over budget, every slot loses its far tiles together. No slot loses everything while another keeps all.
  - The steps "old levels, then the ring, then far visible tiles" stay.
  - The budget note stays viewer-wide.
- **Errors per slot:** `last_error` carries the stamp, so the row can show it. The viewer-wide hint still shows memory notes.
- **Dashed** (§4): a `dash` field on the drawn layer.

### 3.5 Budget, with no limit (ruling 5)

- The budget stays 256 MiB.
- A 512×512 uint32 tile is 1 MiB. One mask on a 1920×1080 view needs 12–20 MiB for the visible tiles and 30–42 MiB with the ring (codex). About 6 masks fit with the ring; about 12–20 without it.
- Past that, the planner drops the ring first, then far visible tiles of every slot together (§3.4), and the hint says "mask memory is full".
- This is the behaviour the user accepted with "no limit". The budget is not raised.
- Not covered by the budget, as today: CPU copies, read scratch and memory-only pyramids.

## 4. Dashed outlines (consulted: codex `codex_dash_1.out`, Fable 5.1)

**Fable** recommends a small change to the outline shader.
- The local boundary direction is found inside the existing neighbour loop, at no extra cost, and quantised to 4 directions.
- Dashes are cut by position along that direction, anchored to the slide, with a fixed length on screen.
- About 20 shader lines, 3 uniforms and a `dash` field. The CPU reference `outline_reference` and the copy in `ui/shaders/` are updated to match.
- Even on straight and gently curved edges.
- Irregular dash lengths on cells under ~15 px on screen.
- Dashes slide along an edge while zooming, but keep their length.

**Codex:**
- Fable's method is not true contour dashes.
- On small cells the direction estimate may jump while panning (flicker).
- Even dashes like Step1's need contours extracted as lines with their arc length, drawn as GPU strokes: a separate, larger block.
- The per-layer cost (the existing outline loop, up to 288 fetches per pixel at radius 8) must be measured on the real machine.

**Plan:** Fable's version is built in this block, then judged on the user's slide by panning and zooming small and large cells, with the frame time measured.
- If the user accepts the look, it stays.
- If not, `Dashed` is left out of this block and true contour dashes become their own block.
- **Decision for the user:** accept this trial (§8).

## 5. Files (closed whitelist) and rollback

**Rust:**
- `native/fusion_viewer_core/src/label_source.rs`
- `label_pool.rs`
- `label_plan.rs`
- `label_read.rs` (key only)
- `label_store.rs` (`dash` field)
- `viewer/labels.rs`
- `py/viewer.rs`
- `py/label_source.rs`
- `py/labels.rs`
- `render/labels.rs`
- `shaders/step1_gpu_labels.frag` (and `ui/shaders/step1_gpu_labels.frag`)
- Rebuild and install the wheel.

**Python:**
- `ui/step3_core_labels.py`
- `ui/step3_label_binding.py`
- `ui/step3_core_label_source.py` (if needed)
- `ui/step1_viewer_mount.py` (mask section only)
- `ui/step1_core_labels.py` (L1 surface, if needed)
- `ui/step1_gpu_layer.py` (dash uniform for the Python GPU layer, if L1 needs it)
- `ui/step3_mask_bar.py` (panel without Show/Fill, plus Dashed)
- new `ui/step3_results_window.py`
- `ui/step3_page.py`
- `ui/main_window.py` (Step3 sections at ~2397–2620, `_rm_release_paths`, the corner)
- `core/step3_masks.py` (`outline_reference` dash only)

**Tests:**
- updated: `tests/test_v16_5_r3b_parity.py`, `test_step3_label_binding.py`, `test_step3_mask_wiring.py`, `test_step3_patch_strip.py`, `test_step3_label_render.py`, `test_v16_5_r3_labels.py`, and any other Step3 label test that assumes two kinds;
- new: `tests/test_step3_multi_results.py`.

**Scripts** (probes that assume two kinds): `scripts/r3_label_gate.py`, `r3b_label_probe.py`, `r3b_pixel_compare.py`.

**Rollback:** one commit per stage (§6). Each stage can be reverted alone; stage 1 needs the old wheel rebuilt.

## 6. Order of work

1. **Rust core:** stamps, the `Vec` slots, the fair planner, per-slot errors, dash. Rust unit tests:
   - N slots;
   - adding or removing a slot keeps the other tiles;
   - fair cutting with 8 and 16 slots;
   - per-slot error;
   - dash on and off in the shader reference.
2. **Bindings and mount** by slot. Tests on **synthetic writable fixtures** (never a real project):
   - two records drawn at once, with pixels checked for every ticked slot;
   - one destination shared by two slots builds once, and both receive the result;
   - adding a record while another's pyramid builds keeps the shown ones;
   - a mix of core-readable and L1 sources;
   - pause/resume during a build.
3. **`Step3ResultsWindow` and the window wiring:**
   - rows, Use, ticks, panels, Load, `×` (only its rows go), retention, default ticks, hint.
   - The old corner drop-down and mask bar are removed.
4. **Real machine** (the user's slide):
   - 1, 2 and 4 records, with xdrag pan/zoom times and per-layer frame time;
   - the label-settle measured separately, because `a9_drive` settle does not wait for masks;
   - overlapping outlines;
   - dashes on small and large cells.
5. Codex code review, then user acceptance, then commit.

## 7. Codex v1 P2s and where they are answered

| P2 | Where |
|---|---|
| 1 shared builds | §3.3 (per destination) |
| 2 budget, fairness | §3.4, §3.5 |
| 3 all-ticks with a cap | ruling 5: no cap, §3.1 |
| 4 active run, Load, delete, retention | §3.1 |
| 5 slot identity, style first, order in the comparison, per-row errors | §3.3, §3.4, §3.2 |
| 6 blanking while building, L1 | §3.3 |
| 7 colour and fill | rulings 3 and 6, §4 |
| 8 whitelist, tests, probes, fixtures | §5, §6 |

P3s: the default (§3.1, a nuclei-only fallback), the opener location (§3.1), the list order and stable colours (§3.1).

## 8. Authorization needed (AGENTS.md)

This block extends existing core state and planning:
- stamps instead of one generation;
- fair planning;
- builds keyed by destination.

It adds no new cache, scheduler or thread. The user is asked to authorize:

1. these core and binding changes;
2. the dashed-outline trial (§4).

## 9. v2.1: answers to codex v2 review (`codex_s3m_plan_2.out`, "revise", 4 P2s)

User authorization (2026-10-30): the core and binding changes of §8 and the dashed trial (§4) are approved. Codex astra high reviews during the work; Fable is not used for this block.

1. **Errors per slot.**
   - The core keeps a map from stamp to error, not one `last_error`. An entry is removed when its stamp leaves the slot list.
   - Results and errors of a stamp not in the current list are dropped, as jobs are.
   - Python reads the map after each frame. Each row shows its own error.
2. **L1 gets the same guarantees as the core:**
   - tiles of unchanged stamps are kept across a new slot list;
   - tile-major, slot-minor planning.
   - It keeps its whole-set fallback and its one serial worker.
3. **`Load…`** ticks the loaded record's Cells, or its Nuclei when it has no cell mask.
   - For a run with several regions, the entry ticked is the one whose region is the current ROI's name if listed, otherwise its first entry.
4. **Whitelist additions** (§5):
   - `ui/main_window.py` hint methods (~2620–2680);
   - `scripts/r3_label_mount_probe.py`, `scripts/r3_biopsy_labels.py`, `scripts/r3_label_probe.py` (dashed parity captures added here);
   - `tests/test_step3_mask_bar.py`, `tests/test_step3_label_mount.py`.

**P3s:**
- **Stamps** come from ONE counter per binding. They are never reused, and are unique across slots: a new stamp for every new source content, including a finished pyramid.
- **Palette** indexing wraps (`% len`).
- **Fairness wording:** over the budget, every shown slot loses its far tiles together. The last group of tiles at one distance may only partly fit, in slot order. With more slots than the budget can give even one tile each (over ~256 at full size), later slots get nothing and the note says so.
- **Default ticks** are applied once, when Step3's list is first filled in a session (scene start). `Use` never ticks or unticks.
- **The dashed trial is already approved**, so §4 and §8 need no further decision.
- **Dashed parity:** `outline_reference(ids, width)` keeps its solid behaviour. A dashed reference takes the same view, zoom, pixel coordinates and direction rule as the shader, and parity is tested on both backends.
