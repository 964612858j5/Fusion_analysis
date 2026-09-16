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
* **Step1's page** (user ruling, 2026-09-16; `docs/step1_rework_plan.md`
  block A) -- TWO columns, held at Step0's own one-to-two ratio, channels to
  picture. The left column is two tabs, `Channels` and `Method & Parameters`;
  the right column is two tabs, `Viewer` and `Patch Results`. Patch Results is
  a TAB beside the picture, never a strip stacked over it. The title line
  reads `Step 1 -- Channel Fusion + Preliminary Segmentation` and carries the
  step's one `Tissue Preview / ROI Navigator` entry at its right end. There is
  no `① ROI / Patch Overview` heading, no `③ Preview` heading over the picture
  and no `Red=cyto Blue=nucleus` legend -- the two mode buttons say which
  picture is up. `Intensity…` sits INSIDE the `Channels` frame, at its top,
  right-aligned, where Step0 keeps it. No outer scroll area wraps the page.
* **Step0 row** -- tick, correction state slot, swatch, name, preview-method
  combo.
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
