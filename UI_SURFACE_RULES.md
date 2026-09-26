# Block01 UI surface rules

**Authority: the user's ruling of 2026-09-15. Highest and longest-lived rule
in this repository. It outranks any architectural argument, review note or
refactor plan, including anything written later that contradicts it.**

## 1. What "making a component global" means

Reusing an existing component's **instance, state, style and lifetime** across
steps. That is the whole of it.

It does **not** authorise:

* a new visible button, window, checkbox, menu, dock, toolbar or panel;
* a change to an existing product interaction in the name of internal purity;
* exposing an internal model concept as a new control.

## 2. Without an explicit request from the user

* Do not add visible UI.
* Do not change what an existing control means.
* If an implementation appears to *require* new UI: **stop and ask first.**

## 3. Why this is written down

Three regressions came out of ignoring it:

* an `f` participation checkbox was added to every Step1 row because the
  domain model had a `fusion_enabled` field. It split one product gesture --
  "use this channel" -- into two controls, and a slide then fused to DAPI
  alone because ticking a channel no longer put it into the fusion;
* `Intensity…` and `Weights…` buttons appeared in the top step bar;
* a whole `Channel Weights` window was built to host weights that Step1's own
  rows already edit.

None of them was asked for. Each cost a review round and a rollback.

A second sweep (2026-09-15) removed five more pieces of Step0 surface the
user had not asked for either: the coarse-level banner, `Compare current
center`, `Reopen full image`, the `Patch geometry saved…` line in the Load
row, and the duplicate Tissue Preview button in the step bar.

## 4. What the current product surface is

* **Step1 row** -- `tick | swatch | name | weight slider + spin`. The tick is
  ONE command: show the channel **and** put it into the fusion; a channel with
  no weight answer enters at 1.0. There is no second participation control.
  **Editing the weight is the same command** (user ruling, 2026-09-16): giving
  a channel a weight shows it and puts it into the fusion, keeping the number
  the user asked for. Nothing here ever takes a channel OUT -- winding a
  weight back to zero is an explicit zero, and the tick is the one gesture
  that removes a channel.
* **Step1's page** (user ruling, 2026-09-16; `docs/step1_rework_plan.md`
  block A) -- TWO columns, holding STEP0'S OWN SHARE of the width for the
  channel column, measured from Step0 at runtime rather than copied as a
  number. (Step0's share is not one-to-two: its column starts at 4/3 of its
  minimum and works out near 0.28 of the work area. A hard 1:2 looked wider
  than Step0 and was rejected on the real machine.) The left column is two tabs, `Fusion` and `Pre-segmentation` (named `Channels` and `Method & Parameters` until 2026-09-24; the FRAME inside `Fusion` is still titled `Channels`, as in Step0);
  the right column is two tabs on screen, `Viewer` and
  `Pre-seg Results` (the montage of a pre-segmentation run, plan block D,
  user ruling 2026-09-24). The old `Patch Results` tab is kept but not
  shown since block E (user ruling 2026-09-25). Patch
  Results is a TAB beside the picture, never a strip stacked over it; so is
  Pre-seg Results: one canvas with every patch of the run (packed in rows,
  its name at its corner, one camera, F fits all, a click selects without
  moving, a double-click on a patch shows it alone and a second one -- or
  one between patches -- shows them all again), composed like the viewer
  from the same channels, Intensity and Overlay / Fusion, with its own
  `Overlay` / `Fusion` buttons above the canvas that are the Viewer's two
  (one command, kept in step; user ruling 2026-09-24). The picture is drawn by
  Step1's own GPU layer (user ruling 2026-09-25) -- Intensity, colours,
  ticks and the mode redraw on the card at once; names, frames and the
  selection are drawn above it. Without a GPU layer the CPU picture is
  used and the terminal says why. In Fusion mode two more toggles follow them,
  `Membrane` and the nucleus channel's name (e.g. `DAPI`), both on,
  in the mode buttons' style but lit in their layer's colour (red / blue):
  either can be left out of the montage's picture (user ruling 2026-09-25)
  -- the Fusion settings, the Viewer and every run are unchanged. The title line
  reads `Step 1 — Channel Fusion + Preliminary Segmentation` and carries the
  step's one `Tissue Navigator` entry at its right end -- Step0's name and
  Step0's look, no icon, in both steps (user ruling, 2026-09-23). The title
  bar is exactly as tall as Step0's Load bar and sits where it does, so the
  two steps' tabs start on the same line. There is
  no `① ROI / Patch Overview` heading, no `③ Preview` heading over the picture
  and no `Red=cyto Blue=nucleus` legend -- the two mode buttons say which
  picture is up. `Intensity…` sits INSIDE the `Channels` frame, at its top,
  right-aligned, where Step0 keeps it. No outer scroll area wraps the page.
  The Channels frame's header row IS Step0's (user ruling, 2026-09-23):
  `Show all` on the left, in Step0's look -- the Step1 tick swept over every
  row, so it means exactly what ticking each row means, and the nucleus is
  left alone -- in the place Step0's `Method ▾` takes and as wide as Step0
  draws it, and `Intensity…` in Step0's exact look right after it, as in
  Step0 (compact, third round). The frame is titled `Channels`, as in Step0.
  There is no `Nucleus: … (weight …)` line and no `Unsaved fusion changes` /
  `Fusion settings saved` or ROI/patch summary. `Save Fusion Settings` (no
  icon) sits in the page's bottom bar where `← Back to Step 0` was -- that
  button is gone from the screen. It is as wide as the Channels frame and
  directly under it, following the column as its handle is dragged, and as
  tall as the BORDER of Step0's Per-Channel Decision frame (not the title
  above it), level with that border. The Channels frame takes the column's
  height, and the two steps' Channels frames start and end on the same
  lines.
  The column behaves as Step0's (fourth round): the same scroll bar, and
  dragging the handle stops where a Step1 row still fits, so the frame's
  right edge, the scroll bar, the weight box and `Save Fusion Settings` are
  never covered by the viewer. The weight box sits at the row's right edge
  and the slider takes the spare width; a read-only weight (the nucleus) is
  centred in its box. The Viewer tab's Patch row carries the
  `Overlay` / `Fusion` mode buttons and then `Load Step0 ROI Result`,
  `Load Previous Step1 Session`, `Save Session` and `⟳ Update` at its right
  end -- there is no row above it for the mode buttons -- and no status row
  under it: `Loading patch…`, `Loading channels…`, warnings and errors go to
  the terminal, and the height goes to the viewer.
  `Load Previous Step1 Session` always opens a file dialog, starting in the
  current ROI's Step1 folder (block S, user ruling 2026-09-26), and says on
  screen what happened: opened, or why not -- a session of another ROI or
  project is refused, saying that opening another project is not supported
  yet (block S2 frozen, user ruling 2026-09-26). `Load weights` restores a
  session's channels only; `Load Previous Step1 Session` the whole scene.
* **Step1's `Pre-segmentation` tab** is three titled frames, `Patches`,
  `Methods` and `Results` (user ruling 2026-09-25), in the look of the
  old controls' boxes. The old Phase1 / Phase2 controls that followed them
  are kept but not shown since block E (user ruling 2026-09-25), and Save
  takes a pre-segmentation result chosen with `Use`, nothing else: old
  Phase 2 parameters, an old session's included, leave it locked.
* **Step1's `Pre-segmentation` tab** opens with the PATCHES strip (user
  ruling, 2026-09-24; `docs/step1_presegmentation_redesign_plan.md` block
  A1): one tile per patch, showing the patch's name and nothing else, all
  ticked by default -- the ticks say which patches a pre-segmentation run
  takes. No hover text. A ticked tile has a small `×` in its top right corner
  that deletes the patch; double-clicking a tile renames it; both are the
  same edit as in the Tissue Navigator. Under the tiles: `Select all`,
  `Select none` and `k/n selected`; with no patches, the line `No patches
  yet — draw them in Step0 or the Tissue Navigator`. The tiles wrap, and past
  three rows the strip scrolls instead of growing. `Random…` sits on its own
  line under them (block A2, user ruling 2026-09-24): count, width and height
  in a small dialog; the patches are drawn inside the ROI polygon, or in the
  tissue without an ROI, never more than 40 % blank and never overlapping, and
  join the patch list as drawn ones do. Only a shortfall is announced.
  `Delete` beside it deletes the TICKED patches after one confirmation
  (user ruling, 2026-09-24): with `Select all` that is every patch; with no
  tick it is disabled. There is no separate "Delete all".
  Under the Patches strip is the METHODS part (plan block B, user ruling
  2026-09-24): a `Methods` caption, one block per method (its name, a summary
  of its values, its combination count, `Edit`, `×`; `No method yet — add one
  with +` when empty), then `+`, `Save plan`, `Load plan…` and the line
  `Total: k patches × m combinations = N tasks`. `+` opens the method dialog:
  the eight methods only (HQ / HQ2 / CDS are hidden, R2; Mesmer disabled with
  the reason where DeepCell is missing), comma-separated values for a list
  parameter, one value otherwise (the StarDist model is fixed and read-only,
  block M), each field checked as typed, the live
  combination count, Save / Cancel. A method already in the plan asks to
  merge (R9, revised 2026-09-24): Yes merges, No keeps the new values as a
  block of their own. `Edit` may change the method as well. `Save plan`
  keeps every patch (position and size) and the ticks; `Load plan…` asks
  whether to bring the patches back, and when there are patches already,
  whether to replace them or keep both.
  Under the total are `Run` and `Stop` and a progress line (plan block C,
  authorised 2026-09-24): Run is off while a run is going (said, not
  silent) and asks first above 10 tasks; Stop cancels what is left. Under
  them is the RESULTS part: a `Results` caption, what is in use, and one row
  per combination of the current run (method, its parameters, `k/n patches`,
  cells, failed / cancelled counts, `out of date`, and `Use`). A LIST ONLY --
  no outlines until the montage view (block D, user ruling (a)). A result
  arriving is never a choice: Save stays disabled until the user presses
  `Use`, and a choice that goes out of date (other pixels or other saved
  Fusion settings) is dropped and Save is disabled again.
  Each row also carries how that combination draws on the montage (block D
  step 2, user ruling 2026-09-25): `Cells` / `Nuclei` (greyed where the
  method makes no such mask), a colour swatch, and a `▾` menu with the line
  width (1 / 1.5 / 2 / 3 px) and dashed cell / nucleus outlines; under the
  `Results` caption, one `Cells` and one `Nuclei` tick (user ruling
  2026-09-25, replacing All / None): ticked shows that kind for every box,
  unticked for none, off at first; half-ticked when the boxes disagree, and
  a click from there shows them all. Each row is framed like a Methods block
  (the same grey border; green while it is in use). Outlines start
  off, in a fixed palette; they are left out while cells are smaller than
  about 3 screen pixels; a failed patch is tagged `failed`, a patch with no
  cell `0 cells`. Many rows scroll rather than squeeze. The rows start at
  the top of the `Results` frame and go down; spare height is below them
  (user ruling 2026-09-26).
  `Save plan` and Step1's `Save Fusion settings` say when they worked
  (user ruling, 2026-09-24), not only when they fail.
* **Patch names** (user ruling, 2026-09-24; plan block P) -- a patch keeps
  its id for life and every view shows its NAME: `P<id>` until the user
  renames it (double-click its row in the Tissue Navigator's patch list, or
  its tile in the Patches strip). Deleting a patch renumbers nobody, and an
  id is never reused. The navigator's patch list shows the name alone.
* **Step0 row** -- tick, correction state slot, swatch, name, preview-method
  combo. THE TICK IS THE PICTURE (user ruling, 2026-09-17): a new slide shows
  the nucleus and no marker, and clicking a row or ticking its box shows THAT
  marker and takes the one that was showing off -- Step0 draws one marker at
  a time. The nucleus is a reference layer with a switch of its own and is
  untouched by either gesture. Step1 is not affected: its tick is the fusion
  command and it shows as many channels as the user ticks.
* **What a step keeps to itself** (user ruling, 2026-09-16; Step1 <-> Step3
  revised by block 2b, user ruling 2026-09-26) -- the display tick and the
  current channel are PER STEP for Step0 and Step2: ticking or selecting in
  one leaves every other step's own answers untouched, and each gets its own
  back on return. **Step1 and Step3 are ONE scope**: they share their ticks,
  their current channel, the weights and the fusion draft, so a fusion built
  in Step3 against the mask is the one Step1 runs again; a Step3 edit is saved
  in the session exactly like a Step1 edit, and the committed snapshot moves
  only when the user saves in Step1. Step2 does not start empty: the first
  time it is opened, and the first time after Step1 commits again, its ticks
  are SEEDED from Step1's committed snapshot -- the channels that snapshot
  enabled, an explicit `0.0` included, and never a channel disabled with a
  weight still in its history. Never from Step0, and never from an
  uncommitted draft. Afterwards Step2's ticks are its own and write back to
  nobody -- and that separation reaches the PAGES, not only the store:
  Step0's consumers act on Step0's scope, Step1's (and Step3's) on Step1's,
  and a page replays its own answers once when its scope comes back on
  screen. A session names the scope it restores, so a Step1 session reloaded
  from Step0 leaves Step0 untouched. Colour, Min/Max/Gamma, the channel order
  and names stay shared, and so do the dock instance, its rows (which are
  therefore the same widgets in every step), its search text and its scroll
  position. Fusion participation and weights belong to Step1's scientific
  draft; Step3 edits that same draft. Entering a step is a redraw, never a
  command: no tick, no weight, no fusion revision and no session save comes
  out of it. The Tissue Preview in Step3 composes Step1's live context.
* **The channel column's width** -- ONE share of the page, with two handles:
  dragging Step0's or Step1's moves both, and a window resize keeps them
  together because the share is normalised. Step0 is written through its own
  left-column mechanism so its hidden peer splitter stays at the same width.
* **Step2 rows** -- tick, swatch, name. Nothing else. **Step3 rows** are
  Step1's rows (block 2b): tick (the fusion command), swatch, name, weight.
* **Step3's page** (block 2c-1, user ruling 2026-09-26) -- Step1's layout
  and look, the channel panel and the viewer only: the title bar with
  `Tissue Navigator`; LEFT, the `Fusion` tab with Step1's `Channels` frame --
  `Show all` (Step1's sweep) and `Intensity…`, the rule, `Reset weights` and
  `Load weights`, then the one dock with Step1's rows, in a host of Step3's
  own; RIGHT, the `Viewer` tab with the `Overlay` / `Fusion` pair (the SAME
  mode as Step1's pair) over the viewer slot, which shows a notice until the
  whole-slide viewer is connected (block 2c-2); `← Back to Step 2` below.
  No Save, no pre-segmentation, no patch strip (a later block), no mask yet.
  The column joins the one channel-column share. A refusal to open Step3 is
  said in a (non-modal) dialog on the page the user is on.
* **Step2's page** (block L2, user ruling 2026-09-25) -- TWO columns: the
  parameter panel on the LEFT, as wide as Step0's and Step1's channel column
  (the same one share: dragging any of the three handles moves all three),
  and the Tile Status Overview with the progress on the right. Each
  parameter row's label is exactly as wide as its text and is never cut;
  the control after it gives way (a drop-down shows its choice elided and
  still opens as wide as its items, a box narrows). A horizontal scroll bar
  appears only when the column is dragged narrower than the panel. Short
  names, so the panel fits the column on a fresh start: `Index:` (was
  `Segmentation Index:`), `Source:`, `Method:` and `Version:` (were
  `Parameter Source:`, `Index method:`, `Parameter version:`); ONE `Method:`
  row is on screen -- from the index it is the index's method, and Step2's
  own method box is kept, hidden; the box `Recovery from .npy` (its old
  title is the tooltip); the hints wrap; the tile line has no VRAM estimate
  (it was not accurate). The
  public `Channels` panel is NOT shown in Step2: its frame and the dock
  mounted in it are kept, hidden, not deleted; Step2's own ticks are kept.
  The Input Data line under `Load zarr info` names no config file (block
  U1, user ruling 2026-09-26: Step1 writes no fusion_config.json).
  Block M (user ruling 2026-09-26): ONE `GPU:` row (`Use GPU if
  available`) for every method, manual or from the index -- unticked, the
  engine runs on the CPU. Mesmer shows Step1's parameters only
  (`maxima_threshold`, `interior_threshold`, `image_mpp`,
  `postprocess_min_size`); nuclear_channel, membrane_channels, input_mode,
  use_gpu, tile_size, overlap, batch_size, normalize_input and the
  percentiles are gone, and so are the `tile size` / `batch size` rows. The
  StarDist model is shown, read-only (`2D_versatile_fluo`). Run on a params
  file with settings the engine does not use lists them with the reason;
  the buttons are `Run per contract` and `Cancel` only.
* **Step1's Save progress** (block L1, user ruling 2026-09-25) -- the modal
  progress dialog alone (with Cancel). The bar that used to sit at the
  bottom of the page is kept, hidden, and never shown; its words go to the
  terminal.
* **Top step bar** -- NO buttons. Three were added there and none was asked
  for; the last of them was a second `🗺 Tissue Preview` beside the one Step0
  already has next to Load.
* **Step0's Channels panel** -- a `Method ▾` button at the top. Its popup
  carries TopHat and cuCIM as lit-or-not toggles with their own parameter
  beside each (radius, sigma), and a `Save` that applies the pair to every
  correction-eligible channel. Both lit = both, neither = original. There is
  no separate `Method Parameters` box: those two numbers live here.
  EVERYTHING IN THE POPUP IS A DRAFT -- the toggles AND the numbers. Nothing
  recomputes until Save. Save applies the METHOD first and then BOTH numbers:
  a parameter is a setting, so an unlit method's number is remembered too --
  what an unlit method does not get is a computation.
  No `Show all` on screen (user ruling, 2026-09-23): Step0 draws one marker at
  a time. The control is kept, hidden, not deleted. `Method ▾` leads the
  header row, where Step1 has `Show all`, and `Intensity…` follows it right
  away, where Step1 has it. No status line under the
  channel list -- no `Ready.` and no run messages -- and the list moves up
  into its place.
* **Step0's Background Correction workspace** -- no `Quantitative Metrics`
  panel (user ruling, 2026-09-23). The patch selector is Step1's -- a
  `Patch ▾` menu with every patch and at most seven inline buttons -- at the
  left end of the viewer's toolbar, before `Original | TopHat | cuCIM`; there
  is no `Preview Patch` box. `Per-Channel Decision` is laid out across one
  row at the bottom, left of `Save`, and nothing else sits under the
  picture. Its four groups -- parameters, method, `Apply`, status -- are
  spaced apart; its frame ends right after the status line, which is as wide
  as the longest `Saved: <channel> <method> (r=…, σ=…)` the slide can produce
  plus one `Background Correction` tab (fifth round), and keeps that width
  (longer messages are elided, whole in the tooltip).
  No `Remap: …` line. There is no status line under `Save` (no `No background
  correction applied …`).
* **Step0's Full Image toolbar** -- no `Compare current center` (a right-click
  opens the comparison and Esc / a right-click leave it), no `Reopen full
  image`, and no `downsampled ×N preview` banner over the picture.
* **Step0's Load row** -- the project's own status. A patch geometry save that
  worked announces nothing there.
* **Weights** -- edited in Step1's rows, and in Step3's, which are the same
  rows (block 2b). There is no separate weight window.
* **Intensity** -- the shared window, opened from Step0's and Step1's own
  existing entries. In BOTH steps that entry lives inside the `Channels`
  frame: it edits the selected channel, so it belongs with the channel list.

## 5. How this rule is enforced

`tests/test_ui_surface_contract.py` fails if any of the removed controls comes
back or if a new top-level window or step-bar button appears. A model's
persistence is not what keeps this rule: this file and that test are.
