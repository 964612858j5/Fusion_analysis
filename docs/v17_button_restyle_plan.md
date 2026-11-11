# Button restyle, every step (v17 backlog item 3)

Status: plan v1.2, 2026-11-10 (codex rounds 1-2 FAIL, P2s taken; round 3 PASS, §6). Approved by the user (TG 2026-11-10); implemented, §7. Continues
`docs/v17_desktop_ui_plan.md` §10 / §10.1 (user rulings 2026-11-03). No code
changed yet.

## 1. What is already done (measured)

Phase 1 (commit 5501cf9, 2026-11-04) delivered most of §10.1:

* `ui/theme/`: tokens (dark default, light), the scoped application sheet,
  `widgets.button_qss(role, metrics)` with the closed role set panel / run /
  danger / ghost / toggle / seg, `panel_box_qss`, `frame_tab_qss`,
  `step_title_html`, `dialogs.confirm_danger`.
* Step bar, step titles (ruling 6), attached tabs (ruling 2), Step0 load row
  and Save, channel rows, Step2 and Step4 in-panel buttons, run buttons green
  and nothing else green (ruling 3), QMessageBox default = primary, others
  outline (ruling 5, application sheet).
* Every button WITHOUT a style of its own already gets the neutral in-panel
  look and hover from the application sheet.

Inventory (2026-11-10, read-only):

* Off-screen build of the main window, every `QPushButton` / `QToolButton`:
  164 built, of which 51 themed (role or `button_qss`), 79 roleless (sheet
  default, already fine), 6 own sheet without colour, **28 with literal
  colours**.
* Source scan: button style sheets with colour literals remain in 12 files.
  They are listed in §2.

## 2. What remains: one role per button

The rule is the one §10.1 already fixed:

* run = the page's run action (green).
* panel = an ordinary in-panel button.
* outline = a secondary button (Browse).
* danger = Stop / Delete / Clear / Cancel-a-run.
* toggle / seg = on/off and one-of-N, checked neutral grey.

The geometry stays: each button's padding, font size and fixed size are
passed as `metrics`, the same values as today (plan §8).

| File | Button | Today | Role |
|---|---|---|---|
| ui/step0/step0_page.py | ROI "✕ Del", Patch "✕ Del" | red literal | danger |
| ui/step0/step0_page.py | ROI/Patch toolbar: ROI mode, Patch mode | ROI/patch colour (data) | keep colour; theme hover only (like the P2–P5 patch buttons) |
| ui/step0/step0_page.py | ROI/Patch toolbar: ✎ Rename | yellow | panel |
| ui/step0/step0_page.py | background-correction method menu "Method: … ▾" | dark literal | panel (QToolButton) |
| ui/step0/step0_page.py | Channels → Method ▾ popup: TopHat, cuCIM (each on/off on its own; both may be lit) | checked yellow | toggle (ruling 4: checked grey). The viewer's Original / TopHat / cuCIM buttons are already `seg` and stay. |
| ui/step0/step0_page.py | Method ▾ popup: "Save" (apply to all eligible channels) | blue literal | panel |
| ui/step0/step0_page.py | channel colour swatch | channel colour | **unchanged** (data) |
| ui/step0/search_ctrl.py | whole-slide correction progress dialog: Cancel (stops the run) | red fill | danger |
| ui/step0/overview_panel.py (Tissue Navigator) | ✓ Close (Enter), Z Undo, 📂 Load ROIs | coloured text | panel |
| ui/step0/overview_panel.py (Tissue Navigator) | D Del ROI, ✕ Clear ROIs, ✕ Clear Patches | red | danger |
| ui/batch_step4_dialog.py | Run Batch | green literal | run |
| ui/step3_mask_bar.py | mask colour swatch | colour (data) | **unchanged** |
| ui/step1_presegmentation/patches_panel.py | patch tile's × (deletes the patch; asks first) | #111 on the patch colour | the tile and its colour stay (data); the × takes the danger colours on hover / pressed, keeping its size and its legibility on the coloured tile |

Out of scope, left as they are:

* Surfaces with no entry on the public path (checked in the source):
  * `ui/step1_5_bg_page.py`: `_go_to_step1_5` is called only by tests.
  * `main_window._btn_back_to_step0`: built hidden.
  * `main_window._btn_next`: never added to a layout and not connected
    (`ui/main_window.py:860`).
  * The old Phase 1 / Phase 2 search panel (`search_ctrl.SearchCtrlPanel`):
    inside `method_params_scroll`, set invisible (block E).
  * The Patch Results tab (`result_grid.py`): `setTabVisible(False)`.
  * `overview_panel.TileSelectDialog` (Use Custom / Cancel / Start Fusion):
    never instantiated.
  * Step0 ROI/Patch toolbar "Delete selected" (`_btn_delete_sel`): built but
    never added to its row (`ui/step0/step0_page.py:769`).
  * `step0_page._file_row`'s Browse: `_file_row` is never called.
  * The channel workbench's Load / Save buttons: Step0 hides them
    (`configure_host_actions`, `ui/step0/step0_page.py:1601`); their other
    host is the Step1.5 page.
  * `Compare methods…`: it belongs to `Step0Inspector`
    (`ui/widgets/channel_dock/editors.py:118`), which is never instantiated.

  They are not restyled, and nothing here brings them back.
* Labels, status lines, progress bars and group-box frames. They are not
  buttons; they can be a later block if the user wants.
* Colours that are data: channel colours, mask colours, patch colours, ROI
  colours, outlines and viewers.

Before each file is changed, its buttons are checked against the public path:
a button the product never shows is left alone and listed in §5. The table is
then corrected, not widened.

## 3. Dialogs (ruling 5, the remainder)

The application sheet already styles every QMessageBox. What remains is the
choices that destroy something but still use a plain Yes/No. Two use
`confirm_danger` today. Found by search, to be switched to `confirm_danger`
(red action, never the default, Esc/Enter keep things):

* `ui/main_window.py` "Load plan — Replace the methods in the plan now?"
* `ui/regions/cores_actions.py` "New candidates — Replace the cores of the
  draft … numbering and marks are cleared".

The question, the answer that proceeds and what happens on each answer stay
the same. Only the button look and the safe default change. Any other
destructive Yes/No found while working through the files is added to §5 for
the user. It is not changed silently.

## 4. Files (closed list) and tests

Files:

* ui/step0/step0_page.py and ui/step0/overview_panel.py: button style
  sheets only.
* ui/step0/search_ctrl.py: the whole-slide correction progress dialog's
  Cancel only.
* ui/batch_step4_dialog.py: the Run Batch style only.
* ui/main_window.py: the Load plan confirmation only.
* ui/step1_presegmentation/patches_panel.py: the tile's × style only.
* ui/regions/cores_actions.py: that one confirmation only.
* ui/theme/widgets.py: only if a role needs a helper for "keep the data
  colour, take the theme hover". No new role and no new token.
* Tests: tests/test_theme.py (extended) and/or
  tests/test_button_restyle.py (new).
* This doc, and docs/v17_desktop_ui_plan.md §10.2 (a pointer).

Nothing else changes: no layout, size, text, signal, enabled-state or
behaviour change, and no new control. UI_SURFACE_RULES is respected.

Tests (off-screen, synthetic, no project read):

1. **Each converted button, by name.** No source-wide literal ban: the
   excluded hidden surfaces keep theirs. Build the widget and check its
   look under both themes:
   * Roles from `ui/theme/widgets.py` (panel / run / danger / toggle / seg): its style
     sheet equals `button_qss(role, metrics)`.
   * `outline`, which is an application-sheet role: `ds == "outline"` and no
     colour in its own sheet.
   * The Method ▾ TopHat / cuCIM toggles are found by their attributes
     (`_method_tophat_btn`, `_method_cucim_btn`), not by text, so the
     viewer's seg buttons cannot satisfy the test.
   * The data-colour buttons (ROI / Patch mode, swatches, the patch tile)
     still carry their data colour.
   * Each button's size hint, or its fixed size, equals today's.
2. **Dialogs.** For the two confirmations:
   * The destructive button is `ds=danger` and is not the default.
   * Enter and Esc both keep things as they are; only clicking the
     destructive button proceeds; the downstream action runs exactly then.
   * The question text stays the same, and the buttons' words stay the same
     where they are Yes/No today (the action word is passed to
     `confirm_danger`).
3. **Regression.** UI tests against HEAD, per test process as usual on the
   Mac.

## 5. Acceptance

* The Mac real machine, dark (default) and light (`BLOCK01_THEME=light`).
  Pages to check:
  * Step0: ROI/Patch toolbar, background correction and its whole-slide
    progress dialog, Tissue Navigator.
  * Step1: the patch tiles' ×, and Load plan.
  * Step4: batch dialog.
  * Regions: new candidates.
* What to check: every changed button hovers; run is green, delete/stop is
  red, the rest neutral; sizes unchanged; the data colours unchanged.
* A list of the buttons left alone, and why, goes to the user with the
  result.

## 6. Plan review (codex astra low)

Round 1, FAIL. Taken:

* P2: the Tissue Navigator's Load ROIs / Clear ROIs were missing; they are
  added.
* P2: the Step1 patch tile's × deletes; it was wrongly listed as a data
  colour. Now the tile stays and the × takes the danger look.
* Advisory, taken: Next, the old search panel, Patch Results and
  TileSelectDialog are unreachable, and are moved out of scope.
* Advisory, taken: the tests now go by named button instead of a
  source-wide ban; outline is checked by its `ds` role; dialogs are tested
  on Enter, Esc and proceed, with the words kept.

Round 2, FAIL. Taken:

* P2: the yellow buttons are Channels → Method ▾'s TopHat / cuCIM
  (independent on/off), not the viewer's Original / TopHat / cuCIM, which
  are already `seg`. They are now named exactly, become `toggle`, and are
  tested by attribute.
* Advisory, taken: "Delete selected", `_file_row`'s Browse, the workbench's
  Load / Save and `Compare methods…` are unreachable. They are moved out of
  scope, and channel_workbench.py and editors.py leave the file list.

Round 3, PASS. One advisory, taken: the test's role list now includes
`toggle`.

## 7. Implementation (2026-11-11)

As §2-§4. Notes:

* The ROI / Patch mode buttons keep their colours. Their rest fill
  `#161616` becomes transparent, the hover is the theme's, and a checked
  button keeps its colour under hover.
* The correction progress box's Cancel had no border. The danger hairline
  is paid for by 1 px less padding (`5px 17px`), so the size stays.
* The Method ▾ TopHat / cuCIM toggles lose the bold of their checked state.
  They have a fixed width, so nothing moves.
* The patch tile's ×: on hover it shows the theme's danger red with a white
  ×, and on press a darker red. The tile itself is unchanged.
* Two existing tests answered the old Yes/No through `QMessageBox.question`:
  `tests/test_step1_method_plan.py::_load` and the `env` fixture of
  `tests/test_tma_f3_page.py`. They now answer `confirm_danger` the same way.
  Each file also gains one call-site test for the No path.
* New: `tests/test_button_restyle.py`, 13 tests:
  * each button by name, in both themes, against its role's sheet;
  * its size against a twin carrying the old sheet;
  * Yes/No danger confirmation on Enter, Esc and Yes.
* Regression against 7e495ec on 22 UI test files, one process per test:
  * New side: 830 tests, 58 not passing; HEAD: 815 tests.
  * Every one of the 58 also fails on HEAD (this Mac's known Qt crashes and
    timeouts).
  * No new failure; the 15 new tests pass.

Code review round 1 (codex astra low), FAIL. Taken:

* P2: the shared `_ts` sheet also styled the hidden "Delete selected"
  button, which the plan leaves alone. It now keeps its old sheet, spelled
  out.
* Advisory, taken: the Enter / Esc test now presses the real keys on the
  real box instead of simulating the click.

Code review round 2: PASS. The hidden button's sheet is byte-identical to
HEAD's, and the real key presses are in place. Next: the user's
real-machine check on the Mac, in dark and light.
