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
| Selected Final | the channel's SELECTED method + params, still computed from raw | `Selected · <method> <param> · Unsaved` / `· Saved` |

**The preview MODE and the SAVE STATE are two independent facts and must be
displayed as such.** A user can have settled on a method without having
pressed Save. So "Selected Final" is not "Saved":

```
Selected · Top-hat radius 25 · Unsaved
Selected · Top-hat radius 25 · Saved
```

And in this phase "Selected Final" still means *raw pixels corrected in the
viewport with the selected parameters* — it does NOT read the saved
artifact (see §3).

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

1. When a Step0 batch background-correction run is REQUESTED, Explore
   immediately stops issuing new work AND cancels what is still queued.
   Tasks that have already started are allowed to finish — a running GPU
   kernel cannot be force-cancelled and this plan does not pretend
   otherwise. The production BG worker starts once Explore's PHYSICALLY
   in-flight work has drained, and the drain duration is recorded.
   **An empty local queue is not proof of quiescence** and must never be
   reported as such.

   Contract debt, stated rather than assumed: `TileScheduler` today exposes
   `request` / `cancel_generation` / `shutdown` and no physical in-flight
   count, so this hand-off cannot be implemented honestly yet. Providing
   that drain interface (the same physical metering
   `notify_on_stale_completion` already gives the multi-channel prefetch
   controller) is a **P2 deliverable**. Until it exists, no claim of
   isolation from production runs may be made.
2. On returning to Explore, work resumes from the CURRENT viewport, not the
   one that was live when it paused.
3. Hiding the tab pauses HOT/COVERAGE but KEEPS the caches.
4. Only a dataset switch or page destruction triggers a full teardown, in
   the order the existing contract pins: `scheduler.shutdown()` (joins
   workers) → `provider.close()` → drop caches.

Explore opens its OWN `RawTileProvider` rather than reusing
`OMETIFFLoader`: the loader is GUI-thread single-handle, and
`set_corrected_zarr_store` changes which pixels it returns. Cost to state
plainly in review: a second set of file handles, a second copy of the
TIFF/Zarr metadata and decoder state, and a second application-level tile
cache. The OS page cache is shared between the two readers, so that is NOT
duplicated.

## 5. Phases

* **P0 — skeleton mount.** New `ui/step0/step0_explore_tab.py` holding the
  stack, built LAZILY on first activation of the tab, with a placeholder
  until a dataset is loaded. Wire build/teardown to the dataset load path
  (`step0_page.py:~2028`, where `OMETIFFLoader` is constructed and
  `_stop_bg_workers()` runs). No prefetch, no mode switching.
  **What P0 displays, stated so the implementer is not left guessing:**
  mode `Original` only, on a SNAPSHOT of `current_channel` taken when the
  stack is built. Channel sync in both directions, and the correction
  preview modes, start at P1.
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

## 6. Acceptance gates, per phase

P0 cannot be judged on behaviour it does not yet have, so the gate is
split.

**P0 (skeleton):**

* the stack builds lazily on first tab activation, and shows a placeholder
  while no dataset is loaded;
* after a dataset switch, not one pixel of the previous dataset is
  displayed;
* teardown happens in the pinned order (`scheduler.shutdown()` →
  `provider.close()` → drop caches), on dataset switch and on destruction;
* only mode `Original` is offered;
* nothing writes to Save, to any config, or to correction numerics.

**P1–P3 (sync, prefetch, measurement):**

* `wrong = 0` — hard gate (no frame may show another channel's pixels);
* near channel switch `full_precise`: **median ≤ 50ms, p95 ≤ 100ms**;
* level-0 switch to an already-cached channel: `raw = 0`, `blank = 0`;
* the first drag after a switch shows no obvious floor exposure, block
  boundary or brightness jump;
* resource isolation from a real Step0 BG run, per §4.

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

## 8. Known pre-existing failures (NOT caused by this work)

Recorded here as debt so a later run does not mistake them for a
regression. Both reproduce identically with `ui/step0/step0_page.py`
restored to its pre-mount version:

* `tests/test_step0_background_correction_outputs.py` — 3 failures
  (`test_method_change_cucim_to_tophat_overwrites_channel`,
  `test_method_change_tophat_to_cucim_overwrites_channel`,
  `test_same_method_param_change_overwrites_channel`);
* the same file HANGS in `test_roi_context_reused_for_same_full_wsi_region`
  (still running after 500s).

Not investigated in this round, by decision. Anyone running the Step0
suites should use `-k "not roi_context"` and expect the 3 failures
until this debt is paid.

## 9. P0 manual acceptance — PASSED 2026-09-01

Run: `python -m block01_v14.main`, real Step0 page, two real slides
(`.../20260210_Ming_Albert_HCC_test_01_Scan1.tiff.ome.tif`, 31680x26880,
57 channels; `.../2025.12.21_Final_28127_22_Slice2_Tonsil.ome.tif`,
31416x28800, 29 channels).

| step | result | evidence |
|---|---|---|
| Explore opened with no dataset | pass | placeholder shown, no `[explore]` line at all — the stack is not built |
| dataset loaded, Explore not opened | pass | `[Loader] …57 channels` with no `building stack` after it |
| first activation | pass | `building stack … channel='CD15'` → `stack ready in 1075 ms`, image present immediately |
| 8–10 tab switches | pass | exactly ONE `building stack` in the whole sequence |
| 3 dataset switches (HCC → tonsil → tonsil → HCC) | pass | every switch logged `tearing stack down` then `building stack: <new path>`, paired one-to-one |
| application close | pass | final line `tearing stack down`; no `QThread: Destroyed while thread is still running`, no fatal error, no read-after-close |

First-build cost: **694–1459 ms** across the runs (well under the ~3.4s the
offscreen smoke measured — the OS page cache is warm by then). The operator
described it as appearing "instantly, faster than the navigator window", so
the synchronous overview read inside the build is acceptable at P0.

The channel snapshot behaved as designed: the two builds took `'CD15'` and
`'TOX'`, the live `current_channel` at build time. P0 has no two-way sync.

**Honest limit:** the log proves the old stack is torn down BEFORE the new
path is bound, and that its provider is closed. Whether a single frame of
the previous dataset ever flashed on screen was **not observed by the
operator** — that is "not seen", not "proven absent". A frame-level check
belongs with the P1–P3 gate, where the `[switch]` diagnostics can assert it.

### Bug found by this acceptance run

`c5890a5`: the Explore view was constructed with the tab as its Qt PARENT,
and `_show_widget` tested parenthood before calling `addWidget` — so the
view was never added to the layout, received no geometry, and the tab came
up BLANK for two minutes while the stack behind it was healthy. All 14
lifecycle tests passed through it, because none of them looked at the
widget. The fix tests layout membership; the new test checks the view is in
the layout, visible, with non-zero geometry, and that the placeholder hides
and comes back. Deliberate lifecycle logging was added in the same commit:
this session had no way to tell whether the build had even started.
