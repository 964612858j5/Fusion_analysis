# Step1 Pre-segmentation: the Methods list scrolls (backlog 9) — plan v1.1

Started by the user (TG) 2026-11-11: "Step1 Pre-segmentation: the Methods list
scrolls". With many methods, the Methods part squeezes its text; it should
scroll with the mouse, like Results.

## 1. Today (read-only)

* `ui/step1_presegmentation/method_blocks.py`, `MethodsPanel`: the method
  blocks sit straight in the panel's layout (`self._list`), above `+`,
  `Save plan` / `Load plan…`, the total, `Run` / `Stop` and the progress line.
  The panel's vertical policy is `Maximum`.
* `ui/main_window.py` puts it in the "Methods" section box (stretch 0), under
  Patches and above the "Results" box (stretch 1).
* `ResultsPanel` already scrolls: its boxes are in a `QScrollArea`, capped at
  their own height (`_fit_scroll`), with the space left over below them.

Measured (offscreen, column 260 px wide, the real `MethodBlock`, 8 methods):
every block is cut to 62 px (`minimumSizeHint`); at that width they need
78–143 px, and their own size hints are 104–260 px. The titles and value
summaries are cut off. That is the reported defect.

## 2. Design (in `MethodsPanel` only)

1. The blocks go into a `QScrollArea` (`list_scroll`), set up like Results:
   * no frame;
   * no horizontal bar;
   * resizable content;
   * the mouse wheel scrolls it (Qt's own behaviour).
2. Each block keeps its full height for the column's width
   (`heightForWidth`), so nothing is squeezed.
3. The list's height:
   * It asks for the blocks' total height at the list's own width (each
     block's `heightForWidth`, plus the spacing). Its maximum is that same
     total, so there is no empty band under the last block.
   * Its minimum is `min(total, 120 px)`.
   * It is recomputed whenever the list's viewport is resized. That covers a
     column resize and the scroll bar appearing or going, which changes the
     width without resizing the panel. It is also recomputed when a block is
     added, removed, edited or loaded.
   * The limits are set, and `updateGeometry()` is called, only when the
     total has changed. `sizeHint()` only reads it.
   * The list is hidden when there is no block; the "No method yet" line
     stays as it is.
4. Everything else stays outside the list, under it and always visible: `+`,
   `Save plan` / `Load plan…`, the total, `Run` / `Stop`, the progress line.
5. Sharing the column with Results: Methods takes up to its full height, and
   Results gets the rest. When the column is too short for both, both shrink
   and each one scrolls.

   Prototype, column 260 px, Results with 6 combinations:

   | Tab height | 2 methods: Methods list / Results list | 8 methods: Methods list / Results list |
   |---|---|---|
   | 500 | 120 / 184 (both scroll) | 120 / 184 (both scroll) |
   | 700 | 132 / 360 | 132 / 360 |
   | 900 | 231 (all shown) / 461 | 332 / 360 |
   | 1400 | 231 (all shown) / 602 | 801 (all shown) / 391 |

6. After `+`, a merge or an Edit, the list scrolls so that block is in view.
   This happens once the new layout has taken effect, on the next turn of
   the event loop, not straight after the insertion. For a block taller than
   the list, its top line (title, `Edit`, `×`) is what comes into view.

Not changed: what a block shows, the plan model, `main_window.py`, Results,
Patches, the theme.

## 3. Files (closed list)

* `ui/step1_presegmentation/method_blocks.py`: `MethodsPanel` layout, the
  list's height, the scroll to a new block. A small `QScrollArea` subclass
  whose size hint is the blocks' height.
* Tests (new): `tests/test_step1_methods_scroll.py`.
* This plan.

## 4. Tests (synthetic, offscreen)

The tab's own three sections: `PatchesPanel`, the Methods panel and a
`ResultsPanel`, each in the product's section box (`MainWindow._section_box`),
Results with stretch 1. Every check is made after the layout has settled
(events processed until the geometry stops changing).

1. With 8 methods, at these sizes in turn: narrow, wide, narrow (180 → 500 →
   260 px), and short, tall, short (500 → 1400 → 600 px). Results is empty in
   one run and filled in the other. At each step:
   * no block is lower than its `heightForWidth`;
   * either the list scrolls, or there is no empty band under the last block
     (judged from the blocks' own geometry);
   * `+`, `Save plan`, `Load plan…`, the total and `Run` / `Stop` lie inside
     the visible column.
2. With 1–2 methods in a tall column: no scroll range and no empty band.
3. With no method: the list is hidden and "No method yet" shows.
4. Real wheel events over a block's title and over its `Edit` button scroll
   the list.
5. Adding the last block, and an Edit that merges into a block scrolled out
   of view, both bring that block into view. For a block taller than the
   list, its title and `Edit` / `×` are in view.
6. An Edit that makes a block's summary wrap onto more lines grows the list.
7. The existing `tests/test_step1_method_plan.py` still pass.

Acceptance by the user on the Mac: add many methods (for example the same
method several times, answering No to merge), check that the text is no
longer cut, that the wheel scrolls the list, and that Run / Stop and the plan
buttons stay where they can be reached.

## 5. Rollback

Revert the commit; nothing is stored or migrated.

## 6. Plan review (codex astra low)

Round 1 FAIL (scratchpad `codex_m9_plan_r1.txt`):
* P2: the list was recomputed only when the panel was resized. A scroll bar
  that appears or goes changes the list's width without resizing the panel,
  so the height went stale (74 px of empty band after narrow → wide →
  narrow). Fixed in §2.3: recompute on the list viewport's resize, and set
  the limits only when the total changes. Prototype re-run: no block is
  squeezed at any step, and the list is exactly as tall as its blocks.
* P3: the tests missed width/height transitions, Patches, and wheel events
  over the blocks' own widgets. §4 now covers them.
* P3: when the scroll to a new block happens was not said. §2.6 now says
  it, and the tests cover it.

Round 2 PASS. One P3 advisory: the prototype's raw "gap" number counted the
layout item, not the blocks; measured from the blocks' own geometry there is
no empty band. The tests measure it from the blocks.

## 7. Implementation (uncommitted, 2026-11-11)

Files as §3: `ui/step1_presegmentation/method_blocks.py` (`_MethodList`,
`LIST_MIN_HEIGHT`, `_fit_list`, `_show_block`, the viewport's event filter),
`tests/test_step1_methods_scroll.py` (11 tests).

* §2.6: the deferred scroll first sends the queued layout requests, so it
  sees the list after the new block is placed.
* The wheel test: offscreen cannot make a system (spontaneous) wheel turn,
  so it checks each step: every widget from the block's title / `Edit` up to
  the list leaves the turn, and the list then scrolls. The real wheel is for
  the Mac acceptance.

Tests: the 11 new ones pass (three runs); `test_step1_method_plan.py` 25
pass. Regression: 12 files that touch the pre-segmentation tab, 221 tests:
200 pass, 21 fail, and the same 21 fail alone on HEAD (none new).

Code review (codex astra low): round 1 PASS, no P1/P2. Two P3 test
advisories (an Edit that merges into a block out of view; a block removed
before its scroll runs), both added.
