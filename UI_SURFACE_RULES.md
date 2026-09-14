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

## 4. What the current product surface is

* **Step1 row** -- `tick | swatch | name | weight slider + spin`. The tick is
  ONE command: show the channel **and** put it into the fusion; a channel with
  no weight answer enters at 1.0. There is no second participation control.
* **Step0 row** -- tick, correction state slot, swatch, name, preview-method
  combo.
* **Step2 / Step3 rows** -- tick, swatch, name. Nothing else.
* **Top step bar** -- `🗺 Tissue Preview` only.
* **Weights** -- edited in Step1's rows. There is no separate weight window.
* **Intensity** -- the shared window, opened from Step0's and Step1's own
  existing entries.

## 5. How this rule is enforced

`tests/test_ui_surface_contract.py` fails if any of the removed controls comes
back or if a new top-level window or step-bar button appears. A model's
persistence is not what keeps this rule: this file and that test are.
