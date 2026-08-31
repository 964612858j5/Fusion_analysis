# v15 — Mounting Explore inside Step0 (plan, approved for P0)

Status: plan approved 2026-09-01; P0 not yet implemented.
Supersedes: `docs/v15_step0_explore_integration.md` (its §1–§2 describe a
three-layer mosaic canvas; the viewer has since moved to per-tile items in
fixed world coordinates, a corrected floor, and multi-channel prefetch).
Evidence this plan rests on: `docs/benchmarks/2026-09-01_channel_switch_latency_manual.md`,
`docs/benchmarks/2026-09-01_57ch_coverage_long_dwell.md`.

## 0. What is being mounted

The v15 viewer stack — `RawTileProvider` → `CorrectionCompute` →
`TileScheduler` → `ExploreController`/`ExploreView`, plus
`MultiChannelPrefetchController` — inside Step0, so background correction
can be judged on the WHOLE slide at full resolution instead of on patches.

## 1. Mount shape

**P0 mounts a third tab** on `step0_page.py`'s `self._step0_tabs`
(`ui/step0/step0_page.py:349`, today: "Background Correction" and
"Channel Remap"). It is purely additive; neither existing tab is touched.

> **The third tab is a v15 integration trial entry point, not the final
> UI.** It exists to prove lifecycle, dataset switching and behaviour under
> real Step0 load at the lowest possible risk.

**Final shape** (a later phase, not P0): Explore becomes a VIEW MODE inside
the Background Correction workspace —

```
Background Correction
├── Explore
├── Compare
└── Pinned patches
```

— because the user must not have to bounce between a top-level "correction
controls" tab and a top-level "whole-slide preview" tab to do one job.
"Channel Remap" stays a separate top-level tab.

## 2. Preview modes (replaces the old "final selected only" contract)

Explore exists to help the user DECIDE a correction method, so it cannot
show only what has already been decided. Four explicit display states:

| mode | pixels | badge |
|---|---|---|
| Original | raw, no correction | — |
| Top-hat Preview | top-hat at the CURRENT slider radius | `Preview · unsaved` |
| cuCIM Preview | cuCIM at the CURRENT slider sigma | `Preview · unsaved` |
| Selected Final | the channel's decided method + params | `Saved · <method> <param>` |

Rules:

* Top-hat / cuCIM preview follow the live sliders (`_tophat_slider`,
  `_cucim_slider`), updating as they move.
* "Selected Final" is selectable ONLY when the channel has a decision in
  `_channel_decisions`.
* With no decision yet, the default mode is the method the user is
  currently adjusting — NOT a silent fall back to raw.
* HOT prepares BOTH methods for neighbouring channels, each at that
  channel's own current parameters (`HOT_METHODS = ("tophat", "cucim")`
  already does this; `ChannelCorrectionSpec` already carries per-channel
  `tophat_radius` / `cucim_sigma`).

This is the same four-way contract Compare will need later
(Original | Top-hat | cuCIM | Final), so it is defined once, here.

## 3. Save / stage boundary

Explore performs `raw → viewport correction preview` only. Remap continues
to read ONLY the saved corrected artifact. The boundary is unchanged.

**A Save must NOT invalidate Explore's cache.** Explore's cache identity is
`raw source + channel + method + params + algorithm version`; a Save changes
none of them. On `stage_invalidated(channel, "corrected_saved")` Explore
updates the channel's status badge (`Saved · Top-hat radius 25`) and nothing
else. Reading and verifying `corrected_saved` pixels is a separate later
phase (it needs `SourceIdentity.stage="corrected_saved"` plus
`corrected_artifact`).

## 4. Resources and discipline

Accepted defaults: corrected cache **2 GB**, raw cache **512 MB**,
**8 I/O workers**, **4 compute workers**, **HOT on**, **COVERAGE off**
(measured: at 512 MB a long COVERAGE dwell evicted 1305 of the 1817 tiles it
produced and left 0/57 channels ready). 2 GB covers the current channel plus
its ±2 neighbourhood in both methods. No hardware-adaptive cache policy for
now.

Discipline, in order of strictness:

1. When Step0 starts a real batch background-correction run, Explore
   **stops issuing new work**. Already-started physical tasks are allowed to
   finish quickly — we do NOT claim a GPU kernel can be force-cancelled.
2. On returning to Explore, work resumes from the CURRENT viewport, not the
   one that was live when it paused.
3. Hiding the tab pauses HOT/COVERAGE but KEEPS the caches.
4. Only a dataset switch or page destruction triggers a full teardown, in
   the order the existing contract pins: `scheduler.shutdown()` (joins
   workers) → `provider.close()` → drop caches.

Explore opens its OWN `RawTileProvider` rather than reusing
`OMETIFFLoader`: the loader is GUI-thread single-handle, and
`set_corrected_zarr_store` changes which pixels it returns. Cost to state
plainly in review: a second set of file handles and page cache for the same
file.

## 5. Phases

* **P0 — skeleton mount.** New `ui/step0/step0_explore_tab.py` holding the
  stack, built LAZILY on first activation of the tab, with a placeholder
  until a dataset is loaded. Wire build/teardown to the dataset load path
  (`step0_page.py:~2028`, where `OMETIFFLoader` is constructed and
  `_stop_bg_workers()` runs). No prefetch, no mode switching yet.
* **P1 — selection adapter.** Two-way sync of `current_channel` and the
  preview mode / sliders, with a re-entrancy guard so an Explore-side
  channel change never triggers Step0's patch preview recompute. Modelled on
  the existing `ui/step0/step0_dock_adapter.py`.
* **P2 — prefetch and budgets.** HOT with per-channel specs from the live
  sliders and decisions; COVERAGE off; the pause/resume discipline of §4.
* **P3 — in-app measurement and manual acceptance.** Promote the demo's
  `[switch]` diagnostics into a reusable module and expose them behind a
  hidden switch.

Explicit non-goals for P0: Compare, the final view-mode re-layout, browsing
saved-corrected pixels, replacing the existing patch preview, OpenGL, and
any change to Save, configs or correction numerics.

## 6. Acceptance gate

* `wrong = 0` — hard gate (no frame may show another channel's pixels).
* Near channel switch `full_precise`: **median ≤ 50ms, p95 ≤ 100ms**.
* Level-0 switch to an already-cached channel: `raw = 0`, `blank = 0`.
* While a real Step0 BG worker run is in progress, Explore issues no new
  work and does not compete with the production task.
* After a dataset switch, not one pixel of the previous dataset is
  displayed.
* The first drag after a switch shows no obvious floor exposure, block
  boundary or brightness jump.

## 7. Test plan

Offscreen Qt tests with fakes (no real WSI in unit tests):

* teardown ORDER on dataset switch and on page destruction;
* no read after close, in every handle mode (existing contract);
* the re-entrancy guard: an Explore channel change does not re-enter Step0's
  preview recompute;
* pause on hide / on BG run start: no new scheduler requests are issued;
* resume uses the CURRENT viewport;
* a Save (`stage_invalidated`) changes the badge and evicts NOTHING;
* preview-mode selection maps to the expected `(method, params)` and
  "Selected Final" is unavailable without a decision.
