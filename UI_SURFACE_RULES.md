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
  than Step0 and was rejected on the real machine.) The left column is two tabs, `Channels` and `Method & Parameters`;
  the right column is two tabs, `Viewer` and `Patch Results`. Patch Results is
  a TAB beside the picture, never a strip stacked over it. The title line
  reads `Step 1 — Channel Fusion + Preliminary Segmentation` and carries the
  step's one `Tissue Preview / ROI Navigator` entry at its right end. There is
  no `① ROI / Patch Overview` heading, no `③ Preview` heading over the picture
  and no `Red=cyto Blue=nucleus` legend -- the two mode buttons say which
  picture is up. `Intensity…` sits INSIDE the `Channels` frame, at its top,
  right-aligned, where Step0 keeps it. No outer scroll area wraps the page.
* **Step0 row** -- tick, correction state slot, swatch, name, preview-method
  combo.
* **What a step keeps to itself** (user ruling, 2026-09-16) -- the display
  tick and the current channel are PER STEP, in ALL FOUR: ticking or selecting
  in one leaves every other step's own answers untouched, and each step gets
  its own back on return. Step2 and Step3 do not start empty: the first time
  each is opened, and the first time after Step1 commits again, its ticks are
  SEEDED from Step1's committed snapshot -- the channels that snapshot
  enabled, an explicit `0.0` included, and never a channel disabled with a
  weight still in its history. Never from Step0, and never from an uncommitted
  draft. Afterwards those ticks are that step's own and write back to nobody -- and that separation reaches the PAGES, not only the
  store: Step0's consumers act on Step0's scope, Step1's on Step1's, Step3 on
  the shared pair alone, and a page replays its own answers once when it comes
  back on screen. A session names the scope it restores, so a Step1 session
  reloaded from Step0 leaves Step0 untouched. Colour, Min/Max/Gamma, the
  channel order and names stay shared, and so do the dock instance, its rows
  (which are therefore the same widgets in both steps), its search text and
  its scroll position. Fusion participation and weights belong to Step1's
  scientific draft alone. Entering a step is a redraw, never a command: no
  tick, no weight, no fusion revision and no session save comes out of it.
  (Step2 and Step3 still share the old pair; they were not part of this
  ruling.)
* **The channel column's width** -- ONE share of the page, with two handles:
  dragging Step0's or Step1's moves both, and a window resize keeps them
  together because the share is normalised. Step0 is written through its own
  left-column mechanism so its hidden peer splitter stays at the same width.
* **Step2 / Step3 rows** -- tick, swatch, name. Nothing else.
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
* **Step0's Full Image toolbar** -- no `Compare current center` (a right-click
  opens the comparison and Esc / a right-click leave it), no `Reopen full
  image`, and no `downsampled ×N preview` banner over the picture.
* **Step0's Load row** -- the project's own status. A patch geometry save that
  worked announces nothing there.
* **Weights** -- edited in Step1's rows. There is no separate weight window.
* **Intensity** -- the shared window, opened from Step0's and Step1's own
  existing entries. In BOTH steps that entry lives inside the `Channels`
  frame: it edits the selected channel, so it belongs with the channel list.

## 5. How this rule is enforced

`tests/test_ui_surface_contract.py` fails if any of the removed controls comes
back or if a new top-level window or step-bar button appears. A model's
persistence is not what keeps this rule: this file and that test are.
